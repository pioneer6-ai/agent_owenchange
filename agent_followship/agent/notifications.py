"""
Notification and messaging layer for patient communication.

This module provides multi-channel notification capabilities,
allowing the agent to reach patients through their preferred
communication method (SMS, WhatsApp, Email, Phone).

WhatsApp (Twilio) configuration
--------------------------------
Set these environment variables to enable real sending:

    TWILIO_ACCOUNT_SID   — your Twilio Account SID
    TWILIO_AUTH_TOKEN    — your Twilio Auth Token
    TWILIO_WHATSAPP_FROM — your Twilio WhatsApp sandbox/production number
                           in E.164 format, e.g. "+14155238886"

When these variables are absent the channel falls back to mock mode
(prints to stdout, always returns True).
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional
import os

from core.models import PatientRecord, ContactChannel, FollowUpCase


class NotificationChannel(ABC):
    """
    Abstract base class for all notification channels.
    
    Each channel implementation handles the specifics of sending
    messages through that medium (API calls, formatting requirements, etc.).
    """

    @abstractmethod
    def send(self, patient: PatientRecord, message: str) -> bool:
        """
        Send a message to a patient through this channel.
        
        Args:
            patient: Patient to contact
            message: Message content to send
            
        Returns:
            True if message was sent successfully, False otherwise
        """
        pass

    @abstractmethod
    def get_channel_type(self) -> ContactChannel:
        """
        Get the channel type identifier.
        
        Returns:
            ContactChannel enum value
        """
        pass


class SMSChannel(NotificationChannel):
    """
    SMS notification channel implementation.
    
    In production, this would integrate with SMS gateway services
    like Twilio, AWS SNS, or similar providers.
    """

    def __init__(self, api_key: Optional[str] = None):
        """
        Initialize SMS channel.
        
        Args:
            api_key: API key for SMS service (e.g., Twilio)
        """
        self.api_key = api_key

    def send(self, patient: PatientRecord, message: str) -> bool:
        """Send SMS message to patient."""
        phone_number = patient.contact_info.get(ContactChannel.SMS)
        
        if not phone_number:
            print(f"[SMS] No phone number for patient {patient.patient_id}")
            return False
        
        # Mock implementation - in production, would call SMS API
        print(f"[SMS] Sending to {phone_number}:")
        print(f"      {message}")
        
        # Simulate successful send
        return True

    def get_channel_type(self) -> ContactChannel:
        """Return SMS channel type."""
        return ContactChannel.SMS


class WhatsAppChannel(NotificationChannel):
    """
    WhatsApp notification channel with Twilio Business API support.

    Operates in two modes determined at construction time:

    LIVE mode  — All three Twilio env-vars are present.
                 Messages are delivered via the Twilio WhatsApp API.
                 Recipient numbers must be in E.164 format (+601XXXXXXXX).

    MOCK mode  — Env-vars are absent (or mock=True is passed).
                 Messages are printed to stdout; always returns True.
                 Safe for development and demos.

    Environment variables (live mode):
        TWILIO_ACCOUNT_SID
        TWILIO_AUTH_TOKEN
        TWILIO_WHATSAPP_FROM   e.g. "whatsapp:+14155238886"
    """

    def __init__(
        self,
        account_sid: Optional[str] = None,
        auth_token: Optional[str] = None,
        from_number: Optional[str] = None,
        mock: bool = False,
    ):
        """
        Initialize the WhatsApp channel.

        Credentials are taken from constructor args first, then from
        environment variables.  If neither is available the channel
        runs in mock mode automatically.

        Args:
            account_sid:  Twilio Account SID (overrides TWILIO_ACCOUNT_SID)
            auth_token:   Twilio Auth Token  (overrides TWILIO_AUTH_TOKEN)
            from_number:  Sending number in E.164, e.g. "+14155238886"
                          (overrides TWILIO_WHATSAPP_FROM)
            mock:         Force mock mode even when credentials exist
        """
        self._account_sid = account_sid or os.getenv("TWILIO_ACCOUNT_SID", "")
        self._auth_token  = auth_token  or os.getenv("TWILIO_AUTH_TOKEN",  "")
        self._from_raw    = from_number or os.getenv("TWILIO_WHATSAPP_FROM", "")

        self._mock = mock or not all([
            self._account_sid,
            self._auth_token,
            self._from_raw,
        ])

        if not self._mock:
            # Lazy-import so missing twilio package only errors when live mode
            # is actually used, not when the module is imported in mock setups.
            try:
                from twilio.rest import Client  # type: ignore
                self._client = Client(self._account_sid, self._auth_token)
                self._from_number = (
                    self._from_raw
                    if self._from_raw.startswith("whatsapp:")
                    else f"whatsapp:{self._from_raw}"
                )
                print("[WhatsApp] Live mode: Twilio client initialised.")
            except ImportError:
                print(
                    "[WhatsApp] WARNING: twilio package not installed. "
                    "Falling back to mock mode.  Run: pip install twilio"
                )
                self._mock = True

    # ------------------------------------------------------------------
    # NotificationChannel interface
    # ------------------------------------------------------------------

    def send(self, patient: PatientRecord, message: str) -> bool:
        """
        Send a WhatsApp message to a patient via their stored number.

        Looks up the patient's WhatsApp contact first; falls back to the
        SMS number if WhatsApp is not recorded.

        Args:
            patient: PatientRecord with contact_info
            message: Text to send

        Returns:
            True if the message was accepted/sent, False on failure
        """
        number = (
            patient.contact_info.get(ContactChannel.WHATSAPP)
            or patient.contact_info.get(ContactChannel.SMS)
        )
        if not number:
            print(f"[WhatsApp] No number found for patient {patient.patient_id}")
            return False

        return self.send_to_number(number, message)

    def get_channel_type(self) -> ContactChannel:
        """Return WhatsApp channel type."""
        return ContactChannel.WHATSAPP

    # ------------------------------------------------------------------
    # Extended API used by WhatsAppAlertAgent
    # ------------------------------------------------------------------

    def send_to_number(self, to_number: str, message: str) -> bool:
        """
        Send a WhatsApp message to an arbitrary E.164 number.

        This method is used by WhatsAppAlertAgent so it can reach
        staff numbers that are not stored as PatientRecord contacts.

        Args:
            to_number: Recipient phone in E.164 format, e.g. "+60123456789"
            message:   Text to send

        Returns:
            True on success, False on failure
        """
        if self._mock:
            return self._mock_send(to_number, message)
        return self._twilio_send(to_number, message)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _twilio_send(self, to_number: str, message: str) -> bool:
        """Send via Twilio WhatsApp API."""
        to_wa = (
            to_number
            if to_number.startswith("whatsapp:")
            else f"whatsapp:{to_number}"
        )
        try:
            msg = self._client.messages.create(
                body=message,
                from_=self._from_number,
                to=to_wa,
            )
            print(
                f"[WhatsApp] ✅ Sent to {to_number} "
                f"(SID: {msg.sid}, status: {msg.status})"
            )
            return True
        except Exception as exc:
            print(f"[WhatsApp] ❌ Twilio error sending to {to_number}: {exc}")
            return False

    def _mock_send(self, to_number: str, message: str) -> bool:
        """Print message instead of sending (mock / demo mode)."""
        separator = "─" * 60
        print(f"\n[WhatsApp MOCK] → {to_number}")
        print(separator)
        print(message)
        print(separator + "\n")
        return True

    @property
    def is_live(self) -> bool:
        """True when connected to the real Twilio API."""
        return not self._mock


class EmailChannel(NotificationChannel):
    """
    Email notification channel implementation.
    
    In production, this would integrate with email services
    like SendGrid, AWS SES, Mailgun, or SMTP servers.
    """

    def __init__(self, smtp_config: Optional[dict] = None):
        """
        Initialize email channel.
        
        Args:
            smtp_config: SMTP server configuration
        """
        self.smtp_config = smtp_config or {}

    def send(self, patient: PatientRecord, message: str) -> bool:
        """Send email to patient."""
        email = patient.contact_info.get(ContactChannel.EMAIL)
        
        if not email:
            print(f"[Email] No email address for patient {patient.patient_id}")
            return False
        
        # Mock implementation - in production, would send actual email
        print(f"[Email] Sending to {email}:")
        print(f"       Subject: Dental Appointment Reminder")
        print(f"       {message}")
        
        # Simulate successful send
        return True

    def get_channel_type(self) -> ContactChannel:
        """Return Email channel type."""
        return ContactChannel.EMAIL


class PhoneCallChannel(NotificationChannel):
    """
    Phone call notification channel implementation.
    
    In production, this would integrate with voice services
    like Twilio Voice, Amazon Connect, or trigger manual calls.
    """

    def __init__(self, voice_api_key: Optional[str] = None):
        """
        Initialize phone call channel.
        
        Args:
            voice_api_key: API key for voice service
        """
        self.voice_api_key = voice_api_key

    def send(self, patient: PatientRecord, message: str) -> bool:
        """Initiate phone call to patient."""
        phone_number = patient.contact_info.get(ContactChannel.PHONE_CALL)
        
        if not phone_number:
            print(f"[Phone] No phone number for patient {patient.patient_id}")
            return False
        
        # Mock implementation - in production, would initiate automated call
        print(f"[Phone] Calling {phone_number}:")
        print(f"       (Automated voice message): {message}")
        
        # Simulate successful call
        return True

    def get_channel_type(self) -> ContactChannel:
        """Return Phone channel type."""
        return ContactChannel.PHONE_CALL


class MessageComposerAgent:
    """
    AI-powered message composition agent.
    
    This component generates personalized, context-aware messages
    for patients based on their profile, language preference,
    urgency level, and treatment type.
    
    In production, this would use an LLM (e.g., OpenAI GPT, Claude)
    to generate natural, empathetic messages. For this demo, it uses
    template-based generation.
    """

    def __init__(self, use_llm: bool = False, llm_api_key: Optional[str] = None):
        """
        Initialize message composer.
        
        Args:
            use_llm: Whether to use LLM for message generation
            llm_api_key: API key for LLM service (if use_llm=True)
        """
        self.use_llm = use_llm
        self.llm_api_key = llm_api_key

    def compose(self, case: FollowUpCase, message_type: str = "initial") -> str:
        """
        Generate a personalized reminder message for a patient.
        
        The message is tailored based on:
        - Patient's preferred language
        - Urgency level
        - Treatment type
        - Communication style (formal vs. friendly)
        
        Args:
            case: Follow-up case containing patient information
            message_type: Type of message ("initial", "reminder", "urgent")
            
        Returns:
            Composed message string
        """
        if self.use_llm:
            return self._compose_with_llm(case, message_type)
        else:
            return self._compose_with_template(case, message_type)

    def _compose_with_template(self, case: FollowUpCase, message_type: str) -> str:
        """
        Generate message using template-based approach.
        
        This is a deterministic, rule-based method that ensures
        consistency and compliance while still personalizing content.
        """
        patient = case.patient
        urgency = case.urgency.value
        treatment = case.patient.treatment_type.replace("_", " ").title()
        
        # Select greeting based on time and formality
        greeting = f"Hello {patient.name},"
        
        # Compose main message based on urgency and type
        if message_type == "initial":
            if urgency == "critical":
                body = (
                    f"This is an important reminder about your {treatment.lower()} follow-up. "
                    f"It's been {case.days_overdue} days past your recommended appointment date. "
                    f"Please contact us as soon as possible to schedule your visit. "
                    f"Your dental health is important to us."
                )
            elif urgency == "high":
                body = (
                    f"We noticed you're overdue for your {treatment.lower()} appointment. "
                    f"To maintain your dental health, we recommend scheduling soon. "
                    f"Would you like to book an appointment this week?"
                )
            else:
                body = (
                    f"It's time for your {treatment.lower()} appointment! "
                    f"We'd love to see you soon. "
                    f"Reply with your preferred day and we'll find a time that works."
                )
        elif message_type == "reminder":
            body = (
                f"Following up on our previous message about your {treatment.lower()} appointment. "
                f"We have several time slots available. "
                f"Would you like to schedule a visit?"
            )
        else:  # urgent
            body = (
                f"We're concerned about your overdue {treatment.lower()} appointment. "
                f"Please contact us at your earliest convenience. "
                f"Our team is ready to help you maintain your dental health."
            )
        
        # Add call-to-action
        cta = "Reply to this message or call us to book your appointment."
        
        # Compose complete message
        message = f"{greeting}\n\n{body}\n\n{cta}\n\nBest regards,\nYour Dental Care Team"
        
        return message

    def _compose_with_llm(self, case: FollowUpCase, message_type: str) -> str:
        """
        Generate message using LLM (placeholder for future implementation).
        
        This would send a prompt to an LLM service with context about
        the patient and case, receiving a personalized message in return.
        """
        # Placeholder for LLM integration
        # In production, would call OpenAI API, Claude API, etc.
        
        prompt = f"""
        Generate a friendly, professional reminder message for a dental patient with these details:
        - Patient name: {case.patient.name}
        - Treatment type: {case.patient.treatment_type}
        - Days overdue: {case.days_overdue}
        - Urgency: {case.urgency.value}
        - Language: {case.patient.language}
        - Message type: {message_type}
        
        Keep the message concise (under 160 characters for SMS compatibility),
        warm but professional, and include a clear call-to-action.
        """
        
        # For demo, fall back to template
        return self._compose_with_template(case, message_type)

    def compose_slot_proposal(self, case: FollowUpCase, available_slots: list) -> str:
        """
        Generate a message proposing available appointment slots.
        
        Args:
            case: Follow-up case
            available_slots: List of available dates
            
        Returns:
            Message with slot options
        """
        patient = case.patient
        treatment = case.patient.treatment_type.replace("_", " ").title()
        
        slots_text = "\n".join([
            f"{i+1}. {slot.strftime('%A, %B %d')}"
            for i, slot in enumerate(available_slots[:3])
        ])
        
        message = f"""Hello {patient.name},

We have the following times available for your {treatment.lower()} appointment:

{slots_text}

Please reply with the number of your preferred time, or suggest an alternative.

Best regards,
Your Dental Care Team"""
        
        return message
