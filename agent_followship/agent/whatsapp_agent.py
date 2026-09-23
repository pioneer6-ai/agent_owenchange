"""
WhatsApp Auto-Alert Agent for Patient Follow-up System.

This module judges each patient's situation based on their case data
(urgency, status, conversation history, escalation state) and sends
appropriately tailored WhatsApp messages automatically.

Alert tiers:
  CRITICAL  — Immediate alert, bold language, request same-day contact
  HIGH      — Urgent reminder, strong call-to-action
  MEDIUM    — Standard follow-up reminder
  LOW       — Gentle, friendly nudge
  ESCALATED — Staff-alert message flagging the case for human action
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from core.models import FollowUpCase, PatientRecord, UrgencyLevel, CaseStatus, ContactChannel


# ---------------------------------------------------------------------------
# Situation Assessment
# ---------------------------------------------------------------------------

class SituationJudge:
    """
    Assesses a patient's situation and decides the right alert tier and
    message tone.  Works for both live FollowUpCase objects and the flat
    escalation dicts stored in EscalationHandler.
    """

    # Conversation-log keywords that signal the patient is frustrated/confused
    FRUSTRATION_KEYWORDS = [
        "stop", "don't contact", "do not contact", "leave me alone",
        "confused", "what do you mean", "don't understand", "harassment",
    ]

    # Keywords in escalation reason that warrant a critical staff alert
    CRITICAL_REASON_KEYWORDS = [
        "pain", "swelling", "infection", "bleeding", "emergency",
        "abscess", "fracture", "urgent", "discomfort",
    ]

    def judge_active_case(self, case: FollowUpCase) -> dict:
        """
        Evaluate a live follow-up case and return a situation report.

        Returns:
            {
                "tier":      "critical" | "high" | "medium" | "low" | "skip",
                "reason":    str,                  # human-readable rationale
                "send_to":   "patient" | "staff",  # who receives the message
                "override_channel": bool,          # True = force WhatsApp even if not preferred
            }
        """
        # Already booked or declined → no alert needed
        if case.status in (CaseStatus.BOOKED, CaseStatus.DECLINED):
            return self._skip("Case already resolved (booked/declined)")

        # Check for frustration signals in conversation log
        recent = " ".join(case.conversation_log[-5:]).lower()
        if any(kw in recent for kw in self.FRUSTRATION_KEYWORDS):
            return self._skip("Patient showed frustration/opt-out signals")

        urgency = case.urgency

        if urgency == UrgencyLevel.CRITICAL:
            return {
                "tier": "critical",
                "reason": (
                    f"CRITICAL urgency: {case.days_overdue} days overdue, "
                    f"{case.reminder_count} reminders unanswered"
                ),
                "send_to": "patient",
                "override_channel": True,  # always use WhatsApp for critical
            }

        if urgency == UrgencyLevel.HIGH:
            return {
                "tier": "high",
                "reason": f"HIGH urgency: {case.days_overdue} days overdue",
                "send_to": "patient",
                "override_channel": False,
            }

        if urgency == UrgencyLevel.MEDIUM:
            return {
                "tier": "medium",
                "reason": f"MEDIUM urgency: {case.days_overdue} days overdue",
                "send_to": "patient",
                "override_channel": False,
            }

        # LOW urgency — only nudge if not contacted recently
        if case.last_contacted:
            days_since = (date.today() - case.last_contacted).days
            if days_since < 7:
                return self._skip(f"Low urgency and contacted {days_since} days ago")

        return {
            "tier": "low",
            "reason": f"LOW urgency: routine follow-up, {case.days_overdue} days overdue",
            "send_to": "patient",
            "override_channel": False,
        }

    def judge_escalated_case(self, escalation: dict) -> dict:
        """
        Evaluate an escalation dict from EscalationHandler and return a
        situation report for a staff WhatsApp alert.
        """
        urgency = escalation.get("urgency", "normal")
        priority = escalation.get("priority", "normal")
        reason = escalation.get("reason", "")
        days_overdue = escalation.get("days_overdue", 0)

        # Already resolved?
        if escalation.get("resolved_at"):
            return self._skip("Escalation already resolved")

        # Boost tier if clinical keywords are in the reason
        reason_lower = reason.lower()
        has_clinical_risk = any(kw in reason_lower for kw in self.CRITICAL_REASON_KEYWORDS)

        effective_tier = priority if priority in ("critical", "high") else urgency
        if has_clinical_risk and effective_tier not in ("critical",):
            effective_tier = "high"

        return {
            "tier": effective_tier if effective_tier in ("critical", "high", "medium", "low") else "medium",
            "reason": reason,
            "send_to": "staff",
            "override_channel": True,  # staff alerts always via WhatsApp
        }

    # ------------------------------------------------------------------
    @staticmethod
    def _skip(reason: str) -> dict:
        return {"tier": "skip", "reason": reason, "send_to": None, "override_channel": False}


# ---------------------------------------------------------------------------
# Message Composer (WhatsApp-specific)
# ---------------------------------------------------------------------------

class WhatsAppMessageComposer:
    """
    Composes WhatsApp-optimised messages for each alert tier.

    WhatsApp messages can use basic markdown (*bold*, _italic_) and emoji,
    so these templates are richer than plain SMS copies.
    """

    CLINIC_NAME = "Your Dental Care Team"
    CLINIC_PHONE = "📞 Call us to book"

    def compose_patient_alert(self, case: FollowUpCase, tier: str) -> str:
        """Compose a patient-facing WhatsApp reminder."""
        name = case.patient.name
        treatment = case.patient.treatment_type.replace("_", " ").title()
        days = case.days_overdue

        if tier == "critical":
            return (
                f"🚨 *IMPORTANT – {self.CLINIC_NAME}*\n\n"
                f"Dear *{name}*,\n\n"
                f"Your *{treatment}* follow-up is *{days} days overdue*. "
                f"This is a time-sensitive health matter and we are concerned about your wellbeing.\n\n"
                f"Please reply *YES* or call us today to schedule your appointment immediately.\n\n"
                f"{self.CLINIC_PHONE}\n\n"
                f"_If you have already booked, please ignore this message._"
            )

        if tier == "high":
            return (
                f"⚠️ *Appointment Reminder – {self.CLINIC_NAME}*\n\n"
                f"Hi *{name}*, your *{treatment}* appointment is *{days} days overdue*.\n\n"
                f"We strongly recommend scheduling soon to protect your dental health.\n\n"
                f"Reply *YES* to confirm your interest and we will send you available slots.\n\n"
                f"{self.CLINIC_PHONE}"
            )

        if tier == "medium":
            return (
                f"📅 *Follow-up Reminder – {self.CLINIC_NAME}*\n\n"
                f"Hi {name}! 😊 It's time for your *{treatment}* check-up.\n\n"
                f"You are {days} days past your recommended visit date.\n\n"
                f"Reply *YES* to book an appointment or let us know a convenient time.\n\n"
                f"{self.CLINIC_PHONE}"
            )

        # low
        return (
            f"👋 *Friendly Reminder – {self.CLINIC_NAME}*\n\n"
            f"Hi {name}! Just a gentle nudge that your *{treatment}* appointment "
            f"is coming up ({days} days overdue).\n\n"
            f"Whenever you are ready, we are here to help. Reply or call us to book! 🦷\n\n"
            f"{self.CLINIC_PHONE}"
        )

    def compose_staff_alert(self, escalation: dict, tier: str) -> str:
        """Compose a staff-facing WhatsApp escalation alert."""
        patient_name = escalation.get("patient_name", "Unknown")
        patient_id = escalation.get("patient_id", "?")
        treatment = escalation.get("treatment_type", "unknown").replace("_", " ").title()
        days = escalation.get("days_overdue", 0)
        urgency = escalation.get("urgency", "unknown").upper()
        reason = escalation.get("reason", "No reason provided")
        escalated_at = escalation.get("escalated_at", "")
        try:
            escalated_at = datetime.fromisoformat(escalated_at).strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass

        tier_emoji = {"critical": "🔴", "high": "🟠", "medium": "🟡"}.get(tier, "🔵")

        conv_log = escalation.get("conversation_log", [])
        last_msgs = conv_log[-3:] if conv_log else []
        conv_snippet = "\n".join(f"  › {m}" for m in last_msgs) if last_msgs else "  › (no conversation)"

        return (
            f"{tier_emoji} *ESCALATED CASE ALERT*\n"
            f"─────────────────────────\n"
            f"👤 *Patient:* {patient_name} (ID: {patient_id})\n"
            f"🔬 *Treatment:* {treatment}\n"
            f"📅 *Days Overdue:* {days}\n"
            f"⚡ *Urgency:* {urgency}\n"
            f"🗓 *Escalated At:* {escalated_at}\n\n"
            f"📋 *Reason:*\n_{reason}_\n\n"
            f"💬 *Last Messages:*\n{conv_snippet}\n"
            f"─────────────────────────\n"
            f"Please review and contact this patient manually."
        )


# ---------------------------------------------------------------------------
# Main Agent
# ---------------------------------------------------------------------------

class WhatsAppAlertAgent:
    """
    Orchestrates situation judgement, message composition, and sending
    for both active follow-up cases and escalated cases.

    Usage:
        agent = WhatsAppAlertAgent(whatsapp_channel, staff_number="+60123456789")

        # Send alerts for all active cases
        results = agent.notify_active_cases(active_cases)

        # Send staff alerts for all escalated cases
        results = agent.notify_escalated_cases(escalated_cases)

        # Send alert for a single case
        result = agent.notify_case(case)
    """

    def __init__(
        self,
        whatsapp_channel,                     # WhatsAppChannel instance
        staff_whatsapp_number: Optional[str] = None,
        min_tier_for_patient: str = "low",    # only alert patients at this tier or above
        min_tier_for_staff: str = "medium",   # only alert staff at this tier or above
    ):
        self.channel = whatsapp_channel
        self.staff_number = staff_whatsapp_number
        self.judge = SituationJudge()
        self.composer = WhatsAppMessageComposer()

        # Tier ordering (higher index = more urgent)
        self._tier_order = ["low", "medium", "high", "critical"]
        self._min_patient_idx = self._tier_index(min_tier_for_patient)
        self._min_staff_idx = self._tier_index(min_tier_for_staff)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def notify_case(self, case: FollowUpCase) -> dict:
        """
        Judge a single active case and send a WhatsApp alert if warranted.

        Returns a result dict with keys:
          sent (bool), tier (str), recipient (str), message (str), reason (str)
        """
        situation = self.judge.judge_active_case(case)

        if situation["tier"] == "skip":
            return self._result(False, "skip", case.patient.name,
                                "", situation["reason"])

        tier = situation["tier"]
        if self._tier_index(tier) < self._min_patient_idx:
            return self._result(False, tier, case.patient.name,
                                "", f"Below minimum alert tier ({tier})")

        # Decide recipient number
        whatsapp_number = case.patient.contact_info.get(ContactChannel.WHATSAPP)
        if not whatsapp_number:
            # Fall back to SMS number if available
            whatsapp_number = case.patient.contact_info.get(ContactChannel.SMS)
        if not whatsapp_number:
            return self._result(False, tier, case.patient.name,
                                "", "No WhatsApp/SMS number on record")

        message = self.composer.compose_patient_alert(case, tier)
        success = self.channel.send_to_number(whatsapp_number, message)

        # Log to conversation
        status_word = "sent" if success else "failed"
        case.add_to_log(
            f"[WhatsApp Alert] {status_word.upper()} — tier={tier} "
            f"to {whatsapp_number} at {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        )

        return self._result(success, tier, case.patient.name, message, situation["reason"])

    def notify_escalated_case(self, escalation: dict) -> dict:
        """
        Judge a single escalation dict and send a staff WhatsApp alert.
        """
        situation = self.judge.judge_escalated_case(escalation)

        if situation["tier"] == "skip":
            return self._result(False, "skip", escalation.get("patient_name", "?"),
                                "", situation["reason"])

        tier = situation["tier"]
        if self._tier_index(tier) < self._min_staff_idx:
            return self._result(False, tier, escalation.get("patient_name", "?"),
                                "", f"Below minimum staff alert tier ({tier})")

        if not self.staff_number:
            return self._result(False, tier, escalation.get("patient_name", "?"),
                                "", "No staff WhatsApp number configured")

        message = self.composer.compose_staff_alert(escalation, tier)
        success = self.channel.send_to_number(self.staff_number, message)

        return self._result(success, tier,
                            escalation.get("patient_name", "?"),
                            message, situation["reason"])

    def notify_active_cases(self, cases: list) -> list[dict]:
        """Send WhatsApp alerts for a list of active FollowUpCase objects."""
        results = []
        for case in cases:
            results.append(self.notify_case(case))
        return results

    def notify_escalated_cases(self, escalations: list[dict]) -> list[dict]:
        """Send staff WhatsApp alerts for a list of escalation dicts."""
        results = []
        for esc in escalations:
            results.append(self.notify_escalated_case(esc))
        return results

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _tier_index(self, tier: str) -> int:
        try:
            return self._tier_order.index(tier)
        except ValueError:
            return 0

    @staticmethod
    def _result(sent: bool, tier: str, recipient: str,
                message: str, reason: str) -> dict:
        return {
            "sent": sent,
            "tier": tier,
            "recipient": recipient,
            "message": message,
            "reason": reason,
            "timestamp": datetime.now().isoformat(),
        }
