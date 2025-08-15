from __future__ import annotations

from typing import Optional, Tuple
from aiogram import types
from aiogram.fsm.context import FSMContext


async def set_expected(state: FSMContext, value: str) -> None:
    await state.update_data(expected=value)


def get_expected(data: dict) -> str:
    return data.get("expected") or ""


def detect_payload(msg: types.Message) -> Optional[Tuple[str, Optional[str], Optional[str]]]:
    """
    Returns (media_type, file_id, text_payload) or None.
    text_payload is used when media_type == 'text'.
    """
    if msg.text and not msg.entities:
        return "text", None, msg.text

    ct = msg.content_type
    if ct == types.ContentType.PHOTO and msg.photo:
        return "photo", msg.photo[-1].file_id, None
    
    if ct == types.ContentType.VIDEO and msg.video:
        return "video", msg.video.file_id, None
    
    if ct == types.ContentType.VIDEO_NOTE and msg.video_note:
        return "video_note", msg.video_note.file_id, None
    
    if ct == types.ContentType.ANIMATION and msg.animation:
        return "animation", msg.animation.file_id, None
    
    if ct == types.ContentType.AUDIO and msg.audio:
        return "audio", msg.audio.file_id, None
    
    if ct == types.ContentType.VOICE and msg.voice:
        return "voice", msg.voice.file_id, None
    
    if ct == types.ContentType.DOCUMENT and msg.document:
        return "document", msg.document.file_id, None
    
    if ct == types.ContentType.STICKER and msg.sticker:
        return "sticker", msg.sticker.file_id, None

    if msg.text:
        return "text", None, msg.text

    return None


async def ensure_expected_or_error(
    msg: types.Message,
    state: FSMContext,
    want: str,
    phrases: dict,
) -> bool:
    data = await state.get_data()
    curr = get_expected(data)
    if curr and curr != want:
        mapping = {
            "await_media": phrases["expect_media"],
            "await_kind": phrases["expect_kind"],
            "await_time": phrases["expect_time"],
            "await_interval": phrases["expect_interval_desc_need"],
            "await_title_choice": phrases["expect_title_choice"],
            "await_title_input": phrases["expect_title_input_need"],
        }
        tip = mapping.get(want) or phrases["generic_expect"]
        await msg.answer(tip)
        return False
    return True
