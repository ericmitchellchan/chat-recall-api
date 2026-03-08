"""Email rendering and sending utilities for Chat Recall."""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"

# Default context values applied to every template render
_DEFAULTS: dict[str, str] = {
    "app_name": "Chat Recall",
    "support_email": "support@chatrecall.ai",
}


def render_template(template_name: str, context: dict[str, str] | None = None) -> str:
    """Load an HTML email template and perform string substitution.

    Parameters
    ----------
    template_name:
        Filename of the template (e.g. ``"welcome.html"``).
    context:
        Dictionary of placeholder values. Keys correspond to
        ``{variable_name}`` tokens in the template. Default values for
        ``app_name`` and ``support_email`` are provided automatically but
        can be overridden.

    Returns
    -------
    str
        The rendered HTML string.
    """
    merged: dict[str, str] = {**_DEFAULTS, **(context or {})}

    template_path = TEMPLATES_DIR / template_name
    html = template_path.read_text(encoding="utf-8")

    for key, value in merged.items():
        html = html.replace("{" + key + "}", value)

    return html


async def send_email(to: str, subject: str, html_body: str) -> None:
    """Send a transactional email.

    Parameters
    ----------
    to:
        Recipient email address.
    subject:
        Email subject line.
    html_body:
        Rendered HTML body content.

    .. note::
        This is a placeholder implementation that logs the email.
    """
    # TODO: Integrate with SES or SendGrid for production email delivery.
    logger.info(
        "Email queued — to=%s subject=%r length=%d",
        to,
        subject,
        len(html_body),
    )
