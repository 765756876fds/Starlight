"""Translate legacy reminder intents for the existing GameAlarm subscribers."""
from types import MappingProxyType
from typing import Any

from ..core.event_bus import Event, EventBus


ALARM_INTENT_EVENTS = MappingProxyType({
    "alarm.flash": "alarm.create_flash",
    "alarm.spell": "alarm.create_spell",
    "alarm.ward": "alarm.create_ward",
    "alarm.custom": "alarm.create_custom",
    "alarm.list": "alarm.list",
    "alarm.remaining": "alarm.remaining",
    "alarm.cancel_last": "alarm.cancel_last",
})


def dispatch_alarm_intent(event_bus: EventBus, intent_type: str, params: dict[str, Any]) -> None:
    """Publish synchronously for a known reminder intent; subscribers own feedback."""
    event_type = ALARM_INTENT_EVENTS[intent_type]
    if intent_type == "alarm.flash":
        champion = params.get("champion", "")
        if not champion:
            return
        data = {"champion": champion}
    elif intent_type == "alarm.spell":
        champion = params.get("champion", "")
        spell = params.get("spell", "")
        if not (champion and spell):
            return
        data = {"champion": champion, "spell": spell}
    elif intent_type == "alarm.ward":
        data = {"location": params.get("location", "当前位置")}
    elif intent_type == "alarm.custom":
        data = {
            "title": params.get("title", "自定义提醒"),
            "duration": params.get("duration", 120),
            "content": params.get("content", ""),
        }
    else:
        data = dict(params)
    event_bus.publish(Event(event_type=event_type, data=data, source="voice"))
