from __future__ import annotations
import logging
from datetime import datetime
from typing import Dict, Optional, TYPE_CHECKING

import pytz
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from mautrix.util import markdown

from mautrix.types import (Format, MessageType, TextMessageEventContent, UserID, EventID, RoomID)

from .util import CommandSyntaxError, CommandSyntax, make_pill, parse_date, UserInfo, format_time

try:
    from cron_descriptor import Options, CasingTypeEnum, DescriptionTypeEnum, ExpressionDescriptor
    USE_CRON_DESCRIPTOR = True
except ImportError:
    USE_CRON_DESCRIPTOR = False

if TYPE_CHECKING:
    from .bot import ReminderBot

logger = logging.getLogger(__name__)

class Reminder(object):
    def __init__(
        self,
        bot: ReminderBot,
        room_id: RoomID,
        message: str,
        event_id: Optional[EventID] = None,
        reply_to: Optional[EventID] = None,
        start_time: Optional[datetime] = None,
        recur_every: Optional[str] = None,
        cron_tab: Optional[str] = None,
        subscribed_users: Optional[Dict[EventID, UserID]] = None,
        creator: Optional[UserID] = None,
        user_info: Optional[UserInfo] = None,
        confirmation_event: EventID = None,
        is_agenda: bool = False,
    ):
        """An object containing information about a reminder, when it should go off,
        whether it is recurring, etc.

        Args:
            bot: instance of the main bot (this feels wrong but it works)
            room_id: The ID of the room the reminder should appear in
            message: The text to include in the reminder message
            event_id: The event ID of the message creating the reminder
            reply_to: The event ID of the message the reminder is replying to if applicable, so we can link it in the reply
            start_time: When the reminder should first go off
            recur_every: date string to parse to schedule the next recurring reminder
            cron_tab: cron text
            subscribed_users: dict of subscribed users with corresponding subscription events
            creator: ID of the creator
            user_info: contains timezone and locale for the person who created the reminder
            confirmation_event: EventID of the confirmation message. Store this so it can be redacted when the reminder is removed.
            is_agenda: Agenda items are reminders that never fire.
        """
        self.bot = bot
        self.room_id = room_id
        self.message = message
        self.event_id = event_id
        self.reply_to = reply_to
        self.start_time = start_time
        self.recur_every = recur_every
        self.cron_tab = cron_tab
        self.creator = creator
        self.subscribed_users = subscribed_users if subscribed_users else {}
        self.confirmation_event = confirmation_event
        self.is_agenda = is_agenda

        # Schedule the reminder

        # TODO add agenda
        trigger = None
        self.job = None

        if not is_agenda:
            # Determine how the reminder is triggered.
            # For both once-off and recurring reminders, user date trigger (runs once).
            # Recurring reminders are rescheduled when the job runs, so we don't need to worry about them here.
            if cron_tab:
                try:
                    self.cron_tab = cron_tab.removeprefix("cron")
                    trigger = CronTrigger.from_crontab(self.cron_tab, timezone=user_info.timezone)
                except ValueError as e:
                    raise CommandSyntaxError(f"The crontab `{self.cron_tab}` is invalid. \n\n\t{str(e)}",
                                             CommandSyntax.CRON_EXAMPLE)

            elif recur_every and start_time < datetime.now(tz=pytz.UTC):
                # If a recurring reminder's last fire was missed, this should fix it
                start_time, _ = parse_date(self.recur_every, user_info=user_info)
                trigger = DateTrigger(run_date=start_time, timezone=user_info.timezone)

            elif start_time:
                trigger = DateTrigger(run_date=start_time, timezone=user_info.timezone)

            # Note down the job for later manipulation
            self.job = self.bot.scheduler.add_job(self._fire, trigger=trigger, id=self.event_id)

    async def _fire(self):
        """Called when a reminder fires"""
        logger.debug("Reminder in room %s fired: %s", self.room_id, self.message)

        user_info = await self.bot.db.get_user_info(self.creator)

        # If this is a recurring message, parse the date again and reschedule the reminder
        if self.recur_every:
            start_time, _ = parse_date(self.recur_every, user_info=user_info)
            trigger = DateTrigger(run_date=start_time, timezone=user_info.timezone)
            self.job = self.bot.scheduler.add_job(self._fire, trigger=trigger, id=self.event_id)
            await self.bot.db.reschedule_reminder(start_time=start_time, event_id=self.event_id)


        # Check if the user is rate limited
        reminder_count = user_info.check_rate_limit(max_calls=self.bot.config["rate_limit"],
                                                    time_window=self.bot.config["rate_limit_minutes"])


        # Send the message to the room if we aren't rate limited
        if reminder_count > self.bot.config["rate_limit"]:
            logger.debug(f"User {self.creator} is rate limited skipping reminder: {self.message}")
        else:
            # Build the message with the format "(users to ping) ⬆️(link to the reminder): message text [next run]
            # Note: ️using "⬆️" as a link seems to have trouble rendering in element for android, but "⬆" works and element on my PC still renders it as the emoji
            targets = list(self.subscribed_users.values())
            link = f"https://matrix.to/#/{self.room_id}/{self.event_id}"
            users = " ".join([(await make_pill(user_id=user_id, client=self.bot.client))
                                  for user_id in targets])

            body = f"{users}: [⬆]({link}) {self.message}"

            if self.recur_every or self.cron_tab:
                body += f"\n\nReminding again {self.formatted_time(user_info)}." \
                        f" Reply `!{self.bot.base_command[0]} {self.bot.cancel_command[0]}` to stop."

            # Warn the user before rate limiting happens
            if reminder_count == self.bot.config["rate_limit"]:
                body += f"\n\n*You've reached the rate limit " \
                        f"({self.bot.config['rate_limit']} per {self.bot.config['rate_limit_minutes']} minutes). " \
                        f"Any upcoming reminders might be ignored!*"

            # Create the message, and include all the data necessary to reschedule the reminder in org.bytemarx.reminder
            content = TextMessageEventContent(
                msgtype=MessageType.TEXT, body=body, format=Format.HTML, formatted_body=markdown.render(body))
            content["org.bytemarx.reminder"] = {"id": self.event_id,
                                              "message": self.message,
                                              "reply_to": self.reply_to}

            # Add subscribed users to MSC3952 mentions
            content["m.mentions"] = {"room": True} if "@room" in targets else {"user_ids": targets}

            if self.reply_to:
                content.set_reply(await self.bot.client.get_event(self.room_id, self.reply_to))

            await self.bot.client.send_message(self.room_id, content)

        # If this was a one-time reminder, cancel and remove from the reminders dict
        if not self.recur_every and not self.cron_tab:
            # We set cancel_alarm to False here else the associated alarms wouldn't even fire
            await self.cancel(redact_confirmation=False)


    async def cancel(self, redact_confirmation: bool = False):
        """Cancels a reminder and all recurring instances

        Args:
            redact_confirmation: Whether to also redact the confirmation message
        """
        logger.debug(f"Removing reminder in room {self.room_id}: {self.message}")
        # Delete the reminder from the database
        await self.bot.db.delete_reminder(self.event_id)
        # Delete any ongoing jobs
        if self.job and self.bot.scheduler.get_job(self.job.id):
            self.job.remove()
        if redact_confirmation and self.confirmation_event:
            await self.bot.client.redact(self.room_id, self.confirmation_event)
        self.bot.reminders.pop(self.event_id)

    async def add_subscriber(self, user_id: UserID, subscribing_event: EventID):
        if user_id not in self.subscribed_users.values():
            self.subscribed_users[subscribing_event] = user_id
            await self.bot.db.add_subscriber(reminder_id=self.event_id,
                                             user_id=user_id,
                                             subscribing_event=subscribing_event)

    async def remove_subscriber(self, subscribing_event: EventID):
        if subscribing_event in self.subscribed_users:
            await self.bot.db.remove_subscriber(subscribing_event=subscribing_event)
            del self.subscribed_users[subscribing_event]


    def formatted_time(self, user_info: UserInfo):
        """
        Format the next run time. as
        Cron reminders are formatted using cron_descriptor,
        Args:
            user_info:
        Returns:

        """
        if self.is_agenda:
            return format_time(self.start_time, user_info=user_info, time_format=self.bot.config['time_format'])
        else:
            next_run = format_time(self.job.next_run_time, user_info=user_info, time_format=self.bot.config['time_format'])
            if self.cron_tab:
                # TODO add languages
                if USE_CRON_DESCRIPTOR:
                    return f"{ExpressionDescriptor(self.cron_tab, casing_type=CasingTypeEnum.LowerCase)} (`{self.cron_tab}`), next run {next_run}"
                else:
                    return f"`{self.cron_tab}`, next run {next_run}"
            elif self.recur_every:
                return f"every {self.recur_every}, next run {next_run}"
            else: # once-off reminders
                return next_run

    async def set_confirmation(self, confirmation_event: EventID):
        """ Set the confirmation message so that it can be redacted if the message is deleted"""
        self.confirmation_event = confirmation_event
        await self.bot.db.set_confirmation_event(event_id=self.event_id, confirmation_event=confirmation_event)
