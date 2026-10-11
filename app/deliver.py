"""Email delivery to Send to Kindle. Recipients come only from the user's config entry."""

from __future__ import annotations

import re
import smtplib
import unicodedata
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from .config import Smtp, User

MESSAGE_OVERHEAD = 4096  # headers and the text part


@dataclass(frozen=True)
class Attachment:
    filename: str
    data: bytes
    article_ids: tuple[str, ...] = ()


def safe_filename(title: str, max_len: int = 120) -> str:
    text = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    text = re.sub(r"[^A-Za-z0-9._ -]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return (text[:max_len].rstrip(" .") or "article") + ".epub"


def encoded_size(raw_bytes: int) -> int:
    """Size on the wire: base64 with line breaks, plus part headers."""
    return raw_bytes * 137 // 100 + 1024


def batch(attachments: list[Attachment], smtp: Smtp) -> list[list[Attachment]]:
    """Pack attachments into as few messages as the size and count limits allow."""
    batches: list[list[Attachment]] = []
    size = 0
    for attachment in attachments:
        need = encoded_size(len(attachment.data))
        if need > smtp.max_message_bytes - MESSAGE_OVERHEAD:
            raise ValueError(f"{attachment.filename} exceeds the message size limit")
        if (
            not batches
            or size + need > smtp.max_message_bytes - MESSAGE_OVERHEAD
            or len(batches[-1]) >= smtp.max_attachments
        ):
            batches.append([])
            size = 0
        batches[-1].append(attachment)
        size += need
    return batches


def build_messages(
    user: User, attachments: list[Attachment], smtp: Smtp
) -> list[tuple[EmailMessage, list[Attachment]]]:
    """One message per batch, paired with the attachments it carries."""
    if not user.kindle_addresses:
        raise ValueError(f"user {user.id} has no delivery address")
    messages = []
    groups = batch(attachments, smtp)
    for index, group in enumerate(groups, 1):
        msg = EmailMessage()
        msg["From"] = smtp.sender
        msg["To"] = ", ".join(user.kindle_addresses)
        msg["Subject"] = f"Forenoon ({index}/{len(groups)})"
        msg["Date"] = formatdate(localtime=False)
        msg["Message-ID"] = make_msgid(domain=smtp.sender.rpartition("@")[2] or None)
        msg.set_content("Forenoon delivery.")
        for attachment in group:
            msg.add_attachment(
                attachment.data,
                maintype="application",
                subtype="epub+zip",
                filename=attachment.filename,
            )
        messages.append((msg, group))
    return messages


def send(messages: list[EmailMessage], smtp: Smtp) -> None:
    if not smtp.host or not smtp.sender:
        raise ValueError("smtp.host and smtp.sender must be configured")
    with smtplib.SMTP(smtp.host, smtp.port, timeout=120) as client:
        if smtp.starttls:
            client.starttls()
        if smtp.username:
            client.login(smtp.username, smtp.password)
        for msg in messages:
            client.send_message(msg)
