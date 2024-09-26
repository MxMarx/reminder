# reminder - A maubot plugin to remind you about things.
# Copyright (C) 2020 Tulir Asokan
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
from __future__ import annotations

import re
from itertools import islice
from collections import deque

from typing import Optional, Dict, List, Tuple, TYPE_CHECKING
from datetime import datetime, timedelta
from attr import dataclass
from dateparser.search import search_dates
import dateparser
import logging
import pytz
from enum import Enum

from maubot.client import MaubotMatrixClient
from mautrix.types import UserID, RoomID, EventID
from maubot.handlers.command import  ArgumentSyntaxError

if TYPE_CHECKING:
    from .reminder import Reminder

logger = logging.getLogger(__name__)


class CommandSyntax(Enum):
    REMINDER_CREATE = """
`!{base_aliases} <date> <message>` Adds a reminder
* `!{base_command} 8 hours buy more pumpkins`
* `!{base_command} 2023-11-30 15:00 befriend rats`
* `!{base_command} abolish closed-access journals at 3pm tomorrow`
* `July 2`, `tuesday at 2pm`, `8pm`, `20 days`, `4d`, `2wk`, ...
* Dates doesn't need to be at the beginning of the string, but parsing works better if they are.

`!{base_command} [room] [every] ...`
* `[room]` pings the whole room
* `[every]` create recurring reminders `!{base_command} every friday 3pm take out the trash`

`!{base_command} [room] <cron> <message>` Schedules a reminder using a crontab syntax
* `!{base_command} cron 30 9 * * mon-fri do something` sets reminders for 9:30am, Monday through Friday.
* `!{base_command} cron` lists more examples

You can also reply to any message with `!{base_command} ...` to get reminded about that message.\\
To get pinged by someone else's reminder, react to their message with 👍.
"""

    AGENDA_CREATE = """
`!{agenda_command} [room] <message>` creates an agenda item. Agenda items are like reminders but don't have a time, for things like to-do lists.
    """

    REMINDER_LIST = """
`!{base_command} list [all] [my] [subscribed]` lists all reminders in a room 
* `all` lists all reminders from every room
* `my` lists only reminders you created
* `subscribed` lists only reminders you are subscribed to
    """

    REMINDER_CANCEL = """
Cancel reminders by removing the message creating it, unsubscribe by removing your upvote.\\
Cancel recurring reminders by replying with `!{base_command} {cancel_aliases}` 
* `!{base_command} {cancel_aliases} <ID>` deletes a reminder matching the 4 letter ID shown by `list`
* `!{base_command} {cancel_aliases} <message>` deletes a reminder **beginning with** <message>
    * e.g. `!remind {cancel_command} buy more` would delete the reminder `buy more pumpkins`
"""

    REMINDER_RESCHEDULE = """
Reminders can be rescheduled by replying to the ping with `!{base_command} <new_date>`
"""

    REMINDER_SETTINGS = """
Dates are parsed using your [timezone](https://en.wikipedia.org/wiki/List_of_tz_database_time_zone) and [locale](https://dateparser.readthedocs.io/en/latest/supported_locales.html).
Defaults are `{default_tz}` and `{default_locale}`
* `!{base_command} tz|timezone [new-timezone]` view or set your timezone
* `!{base_command} locale [new-locale]` view or set your locale
"""

    PARSE_DATE_EXAMPLES = "Examples: `Tuesday at noon`, `2023-11-30 10:15 pm`, `July 2`, `6 hours`, `8pm`, `4d`, `2wk`"

    CRON_EXAMPLE = """
```
*	any value
,	value list separator
-	range of values
/	step 

┌─────── minute (0 - 59)
│ ┌─────── hour (0 - 23)
│ │ ┌─────── day of the month (1 - 31)
│ │ │ ┌─────── month (1 - 12)
│ │ │ │ ┌─────── weekday (0 - 6) (Sunday to Saturday)                             
│ │ │ │ │
* * * * * <message>
```

```
30 9 * * *              Every day at 9:30am
0/30 9-17 * * mon-fri   Every 30 minutes from 9am to 5pm, Monday through Friday
0 14 1,16 * *           2:00pm on the 1st and 16th day of the month
0 0 1-7 * mon           First Monday of the month at midnight
```
 """


@dataclass
class UserInfo:
    locale: str = None
    timezone: str = None
    last_reminders: deque = deque()

    def check_rate_limit(self, max_calls=5, time_window=60) -> int:
        """ Implement a sliding window rate limit on the number of reminders per user
        Args:
            max_calls:
            time_window: moving window size in minutes
        Returns:
            The number of calls within the sliding window
        """
        now = datetime.now(pytz.UTC)
        # remove timestamps outside the sliding window
        while len(self.last_reminders) and self.last_reminders[0] + timedelta(minutes=time_window) < now:
            self.last_reminders.popleft()
        if len(self.last_reminders) < max_calls:
            self.last_reminders.append(now)
        return len(self.last_reminders)

class CommandSyntaxError(ValueError):
    def __init__(self, message: str, command: CommandSyntax | None = None):
        """ Format error messages with examples """
        super().__init__(f"{message}")

        if command:
            message += "\n\n" + command.value
        self.message = message

def validate_timezone(tz: str) -> bool | str:
    try:
        return dateparser.utils.get_timezone_from_tz_string(tz).tzname(None)
    except pytz.UnknownTimeZoneError:
        return False

def validate_locale(locale: str):
    try:
        return dateparser.languages.loader.LocaleDataLoader().get_locale(locale)
    except ValueError:
        return False

def parse_date(str_with_time: str, user_info: UserInfo, search_text: bool=False) -> Tuple[datetime, str]:
    """
    Extract the date from a string.

    Args:
        str_with_time: A natural language string containing a date.
        user_info: contains locale and timezone to search within.
        search_text:
            if True, search for the date in str_with_time e.g. "Make tea in 4 hours".
            if False, expect no other text within str_with_time.

    Returns:
        date (datetime): The datetime of the parsed date.
        date_str (str): The string containing just the parsed date,
                e.g. "4 hours" for str_with_time="Make tea in 4 hours".
    """

    # Until dateparser makes it so locales can be used in the searcher, use this to get date order
    date_order = validate_locale(user_info.locale).info["date_order"]

    # Replace "3w" with "3wk" to satisfy dateparser
    str_with_time = re.sub(r"(\b\d+)\s?w\b", r"\1wk", str_with_time, count=1)

    settings = {'TIMEZONE': user_info.timezone,
                'TO_TIMEZONE': 'UTC',
                'DATE_ORDER': date_order,
                'PREFER_DATES_FROM': 'future',
                'RETURN_AS_TIMEZONE_AWARE': True}

    # dateparser.parse is more reliable than search_dates. If the date is at the beginning of the message,
    # try dateparser.parse on the first 8 words and use the date from the longest sequence that successfully parses.
    date = []
    date_str = []
    for i in reversed(list(islice(re.finditer(r"\S+", str_with_time), 8))):
        extracted_date = dateparser.parse(str_with_time[:i.end()], locales=[user_info.locale], settings=settings)
        if extracted_date:
            date = extracted_date
            date_str = str_with_time[:i.end()]
            break

    # If the above doesn't work or the date isn't at the beginning of the string, fallback to search_dates
    if not date:
        extracted_date = search_dates(str_with_time, languages=[user_info.locale.split('-')[0]], settings=settings)
        if extracted_date:
            date_str, date = extracted_date[0]

    if not date:
        raise CommandSyntaxError("Unable to extract date from string", CommandSyntax.PARSE_DATE_EXAMPLES)

    # Round datetime object to the nearest second for nicer display
    date = date.replace(microsecond=0)

    # Disallow times in the past
    if date < datetime.now(tz=pytz.UTC):
        raise CommandSyntaxError(f"Sorry, `{format_time(date, user_info)}` is in the past and I don't have a time machine (yet...)")

    return date, date_str

def pluralize(val: int, unit: str) -> str:
    if val == 1:
        return f"{val} {unit}"
    return f"{val} {unit}s"

def format_time(time: datetime, user_info: UserInfo, time_format: str = "%-I:%M%P %Z on %A, %B %-d %Y") -> str:
    """
    Format time as something readable by humans.
    Args:
        time: datetime to format
        user_info: contains locale and timezone
        time_format:
    Returns:

    """
    now = datetime.now(tz=pytz.UTC).replace(microsecond=0)
    delta = abs(time - now)

    # If the date is coming up in less than a week, print the two most significant figures of the duration
    if abs(delta) <= timedelta(days=7):
            parts = []
            if delta.days > 0:
                parts.append(pluralize(delta.days, "day"))
            hours, seconds = divmod(delta.seconds, 60)
            hours, minutes = divmod(hours, 60)
            if hours > 0:
                parts.append(pluralize(hours, "hour"))
            if minutes > 0:
                parts.append(pluralize(minutes, "minute"))
            if seconds > 0:
                parts.append(pluralize(seconds, "second"))

            formatted_time = " and ".join(parts[0:2])
            if time > now:
                formatted_time = "in " + formatted_time
            else:
                formatted_time = formatted_time + " ago"
    else:
        formatted_time = time.astimezone(
            dateparser.utils.get_timezone_from_tz_string(user_info.timezone)).strftime(time_format)
        formatted_time = "at " + formatted_time
    return formatted_time


async def make_pill(user_id: UserID, display_name: str = None, client: MaubotMatrixClient | None = None) -> str:
    """Convert a user ID (and optionally a display name) to a formatted user 'pill'

    Args:
        user_id: The MXID of the user.
        display_name: An optional display name. Clients like Element will figure out the
            correct display name no matter what, but other clients may not.
        client: mautrix client so get the display name.
    Returns:
        The formatted user pill.
    """
    # Use the user ID as the display_name if not provided
    if client and not display_name:
        if user_id == "@room":
            return '@room'
        else:
            display_name = await client.get_displayname(user_id)

    display_name = display_name or user_id

    # return f'<a href="https://matrix.to/#/{user_id}">{display_name}</a>'
    return f'[{display_name}](https://matrix.to/#/{user_id})'

