"""
Flask web application for the Patient Follow-up Agent.

This module provides a web-based dashboard for monitoring and controlling
the agent, including:
- Real-time case status visualization
- Agent statistics and metrics
- Manual case management interface
- Audit log viewer
- Simulation controls for testing
"""

import sys
from pathlib import Path

# Allow running this file directly (python web/app.py) as well as
# as a module from the project root (python -m web.app).
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flask import Flask, render_template, jsonify, request
from datetime import date, datetime, timedelta
import json

from agent.orchestrator import FollowUpAgentOrchestrator
from agent.notifications import WhatsAppChannel
from agent.whatsapp_agent import WhatsAppAlertAgent
from core.data_access import MockPatientDataStore, MockCalendarIntegration
from core.models import PatientRecord, ContactChannel, CaseStatus, UrgencyLevel
from core.config import ClinicPolicyConfig
from utils.llm_parser import LLMPatientParser


# Initialize Flask app
app = Flask(__name__)
app.config['JSON_SORT_KEYS'] = False

# Initialize the agent orchestrator
data_store = MockPatientDataStore()
calendar = MockCalendarIntegration()
policy = ClinicPolicyConfig()
agent = FollowUpAgentOrchestrator(data_store, calendar, policy)

# Initialize LLM parser
llm_parser = LLMPatientParser(use_llm=False)  # Set to True with API key for real LLM

# ---------------------------------------------------------------------------
# WhatsApp Alert Agent
# ---------------------------------------------------------------------------
# Reads Twilio credentials from environment variables automatically.
# Set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_FROM to enable
# live sending; otherwise the channel runs in mock (print) mode.
#
# WHATSAPP_STAFF_NUMBER — E.164 number that receives escalation alerts,
#                          e.g. "+60123456789".  Leave unset to skip staff alerts.
import os as _os
_whatsapp_channel = WhatsAppChannel()          # auto-detects live vs mock
_staff_number     = _os.getenv("WHATSAPP_STAFF_NUMBER", "")

whatsapp_alert_agent = WhatsAppAlertAgent(
    whatsapp_channel=_whatsapp_channel,
    staff_whatsapp_number=_staff_number or None,
    min_tier_for_patient="low",    # alert patients from LOW urgency and above
    min_tier_for_staff="medium",   # alert staff from MEDIUM priority and above
)


@app.route('/')
def index():
    """Render the main dashboard page."""
    return render_template('dashboard.html')


@app.route('/api/status')
def get_status():
    """
    Get current agent status and statistics.
    
    Returns:
        JSON with agent statistics and operational metrics
    """
    stats = agent.get_statistics()
    
    # Add additional context
    stats['last_updated'] = datetime.now().isoformat()
    stats['total_patients'] = len(data_store.get_all_active_patients())
    
    return jsonify(stats)


@app.route('/api/cases')
def get_cases():
    """
    Get all active follow-up cases.
    
    Returns:
        JSON array of active cases with patient information
    """
    cases = agent.get_active_cases()
    
    cases_data = []
    for case in cases:
        cases_data.append({
            'patient_id': case.patient.patient_id,
            'patient_name': case.patient.name,
            'treatment_type': case.patient.treatment_type,
            'last_visit': case.patient.last_visit_date.isoformat(),
            'days_overdue': case.days_overdue,
            'urgency': case.urgency.value,
            'status': case.status.value,
            'reminder_count': case.reminder_count,
            'last_contacted': case.last_contacted.isoformat() if case.last_contacted else None,
            'preferred_channel': case.patient.preferred_channel.value,
            'conversation_length': len(case.conversation_log)
        })
    
    return jsonify(cases_data)


@app.route('/api/cases/<patient_id>')
def get_case_detail(patient_id):
    """
    Get detailed information about a specific case.
    
    Args:
        patient_id: Patient identifier
        
    Returns:
        JSON with complete case details including conversation log
    """
    case = agent.get_case_by_patient_id(patient_id)
    
    if not case:
        return jsonify({'error': 'Case not found'}), 404
    
    case_data = {
        'patient_id': case.patient.patient_id,
        'patient_name': case.patient.name,
        'treatment_type': case.patient.treatment_type,
        'last_visit': case.patient.last_visit_date.isoformat(),
        'recall_interval_days': case.patient.recall_interval_days,
        'days_overdue': case.days_overdue,
        'urgency': case.urgency.value,
        'status': case.status.value,
        'reason': case.reason,
        'reminder_count': case.reminder_count,
        'last_contacted': case.last_contacted.isoformat() if case.last_contacted else None,
        'preferred_channel': case.patient.preferred_channel.value,
        'contact_info': {k.value: v for k, v in case.patient.contact_info.items()},
        'conversation_log': case.conversation_log,
        'no_show_history': case.patient.no_show_history,
        'language': case.patient.language
    }
    
    return jsonify(case_data)


@app.route('/api/escalations')
def get_escalations():
    """
    Get all escalated cases requiring staff attention.
    
    Returns:
        JSON array of escalated cases
    """
    escalations = agent.escalation_handler.get_escalated_cases()
    return jsonify(escalations)


@app.route('/api/import-escalated-cases', methods=['POST'])
def import_escalated_cases():
    """
    Import escalated cases from uploaded JSON data.
    
    Accepts JSON with "escalated_cases" key containing a list of
    escalated case objects (matching the EscalationHandler record format).
    
    Request body:
        {
            "escalated_cases": [
                {
                    "patient_id": "...",
                    "patient_name": "...",
                    "treatment_type": "...",
                    "days_overdue": 0,
                    "urgency": "critical",
                    "reason": "...",
                    "priority": "critical",
                    "escalated_at": "...",
                    "conversation_log": []
                }
            ]
        }
        
    Returns:
        JSON with import results
    """
    data = request.get_json()
    
    if not data or 'escalated_cases' not in data:
        return jsonify({'success': False, 'error': 'No escalated_cases data provided'}), 400
    
    try:
        imported_count = 0
        skipped_count = 0
        existing_ids = {e['patient_id'] for e in agent.escalation_handler.get_escalated_cases()}
        
        for case_data in data['escalated_cases']:
            patient_id = case_data.get('patient_id')
            
            # Skip duplicates
            if patient_id in existing_ids:
                skipped_count += 1
                continue
            
            # Ensure required fields
            escalation = {
                'patient_id': case_data.get('patient_id', 'unknown'),
                'patient_name': case_data.get('patient_name', 'Unknown'),
                'treatment_type': case_data.get('treatment_type', 'unknown'),
                'days_overdue': case_data.get('days_overdue', 0),
                'urgency': case_data.get('urgency', 'normal'),
                'reason': case_data.get('reason', 'Manual import'),
                'priority': case_data.get('priority', 'normal'),
                'escalated_at': case_data.get('escalated_at', datetime.now().isoformat()),
                'conversation_log': case_data.get('conversation_log', []),
            }
            
            agent.escalation_handler.escalated_cases.append(escalation)
            existing_ids.add(patient_id)
            imported_count += 1
        
        return jsonify({
            'success': True,
            'imported_count': imported_count,
            'skipped_count': skipped_count,
            'total': len(data['escalated_cases'])
        })
    
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/run-cycle', methods=['POST'])
def run_cycle():
    """
    Trigger a daily agent cycle manually.
    
    Returns:
        JSON with results of the cycle
    """
    try:
        today = date.today()
        processed_cases = agent.run_daily_cycle(today)
        
        return jsonify({
            'success': True,
            'cases_processed': len(processed_cases),
            'timestamp': datetime.now().isoformat()
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/simulate-reply', methods=['POST'])
def simulate_reply():
    """
    Simulate receiving a reply from a patient.
    
    Request body:
        {
            "patient_id": "P001",
            "message": "Yes, I'd like to book"
        }
    
    Returns:
        JSON with result of processing the reply
    """
    data = request.get_json()
    
    if not data or 'patient_id' not in data or 'message' not in data:
        return jsonify({
            'success': False,
            'error': 'Missing patient_id or message'
        }), 400
    
    try:
        agent.handle_incoming_reply(data['patient_id'], data['message'])
        
        return jsonify({
            'success': True,
            'timestamp': datetime.now().isoformat()
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/audit-logs')
def get_audit_logs():
    """
    Get audit logs with optional filtering.
    
    Query parameters:
        - patient_id: Filter by patient ID
        - limit: Maximum number of entries (default: 50)
    
    Returns:
        JSON array of audit log entries
    """
    patient_id = request.args.get('patient_id')
    limit = int(request.args.get('limit', 50))
    
    if patient_id:
        logs = agent.audit_logger.get_patient_history(patient_id)
    else:
        logs = agent.audit_logger.log_entries
    
    # Return most recent logs first, limited
    logs_sorted = sorted(logs, key=lambda x: x['timestamp'], reverse=True)
    return jsonify(logs_sorted[:limit])


@app.route('/api/patients')
def get_patients():
    """
    Get all patients in the system.
    
    Returns:
        JSON array of patient records
    """
    patients = data_store.get_all_active_patients()
    
    patients_data = []
    for patient in patients:
        patients_data.append({
            'patient_id': patient.patient_id,
            'name': patient.name,
            'treatment_type': patient.treatment_type,
            'last_visit_date': patient.last_visit_date.isoformat(),
            'recall_interval_days': patient.recall_interval_days,
            'preferred_channel': patient.preferred_channel.value,
            'no_show_history': patient.no_show_history
        })
    
    return jsonify(patients_data)


@app.route('/api/available-slots/<patient_id>')
def get_available_slots(patient_id):
    """
    Get available appointment slots for a patient.
    
    Args:
        patient_id: Patient identifier
        
    Returns:
        JSON array of available dates
    """
    case = agent.get_case_by_patient_id(patient_id)
    
    if not case:
        return jsonify({'error': 'Case not found'}), 404
    
    slots = agent.scheduler.find_available_slots(case, after=date.today(), limit=10)
    
    return jsonify([slot.isoformat() for slot in slots])


@app.route('/api/book-appointment', methods=['POST'])
def book_appointment():
    """
    Manually book an appointment for a patient.
    
    Request body:
        {
            "patient_id": "P001",
            "appointment_date": "2024-12-15"
        }
    
    Returns:
        JSON with booking result
    """
    data = request.get_json()
    
    if not data or 'patient_id' not in data or 'appointment_date' not in data:
        return jsonify({
            'success': False,
            'error': 'Missing patient_id or appointment_date'
        }), 400
    
    try:
        case = agent.get_case_by_patient_id(data['patient_id'])
        if not case:
            return jsonify({
                'success': False,
                'error': 'Case not found'
            }), 404
        
        appointment_date = date.fromisoformat(data['appointment_date'])
        success, booked_date = agent.scheduler.try_book(case, appointment_date)
        
        if success:
            agent.audit_logger.log_appointment_action(
                case, "manual_booking", booked_date, True
            )
        
        return jsonify({
            'success': success,
            'booked_date': booked_date.isoformat() if booked_date else None
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/config')
def get_config():
    """
    Get current agent configuration.
    
    Returns:
        JSON with policy configuration
    """
    return jsonify({
        'working_hours': policy.working_hours,
        'max_reminders_before_escalation': policy.max_reminders_before_escalation,
        'opt_out_respected': policy.opt_out_respected,
        'high_urgency_threshold_days': policy.high_urgency_threshold_days,
        'critical_urgency_threshold_days': policy.critical_urgency_threshold_days,
        'reminder_interval_days': policy.reminder_interval_days
    })


@app.route('/api/upload-patient-list', methods=['POST'])
def upload_patient_list():
    """
    Upload and parse patient list file using LLM.
    
    Request:
        file: uploaded file (CSV, Excel, JSON, TXT)
        
    Returns:
        JSON with parsed patient data
    """
    if 'file' not in request.files:
        return jsonify({'success': False, 'error': 'No file uploaded'}), 400
    
    file = request.files['file']
    
    if file.filename == '':
        return jsonify({'success': False, 'error': 'No file selected'}), 400
    
    try:
        # Read file content
        file_content = file.read()
        filename = file.filename
        
        # Parse using LLM
        parsed_patients = llm_parser.parse_file(file_content, filename)
        
        return jsonify({
            'success': True,
            'patients': parsed_patients,
            'count': len(parsed_patients)
        })
    
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/import-patients', methods=['POST'])
def import_patients():
    """
    Import parsed patients into the system.
    
    Request body:
        {
            "patients": [parsed patient objects]
        }
        
    Returns:
        JSON with import results including duplicate warnings
    """
    data = request.get_json()
    
    if not data or 'patients' not in data:
        return jsonify({'success': False, 'error': 'No patient data provided'}), 400
    
    try:
        imported_count = 0
        skipped_count = 0
        duplicate_patients = []
        
        # Get existing patients to check for duplicates
        existing_patients = data_store.get_all_active_patients()
        existing_ids = {p.patient_id for p in existing_patients}
        existing_names = {p.name.lower() for p in existing_patients}
        
        for patient_data in data['patients']:
            patient_id = patient_data.get('patient_id', f"P{imported_count+1000}")
            patient_name = patient_data.get('name', 'Unknown')
            
            # Check for duplicate by ID or name
            if patient_id in existing_ids or patient_name.lower() in existing_names:
                skipped_count += 1
                duplicate_patients.append({
                    'name': patient_name,
                    'id': patient_id,
                    'reason': 'Patient already exists in the system'
                })
                continue  # Skip this patient
            
            # Convert to PatientRecord
            contact_info_dict = {}
            for channel_str, value in patient_data.get('contact_info', {}).items():
                # Convert string keys to ContactChannel enum
                try:
                    if channel_str == 'sms':
                        contact_info_dict[ContactChannel.SMS] = value
                    elif channel_str == 'whatsapp':
                        contact_info_dict[ContactChannel.WHATSAPP] = value
                    elif channel_str == 'email':
                        contact_info_dict[ContactChannel.EMAIL] = value
                    elif channel_str == 'phone_call':
                        contact_info_dict[ContactChannel.PHONE_CALL] = value
                except Exception as e:
                    print(f"Warning: Failed to parse contact channel {channel_str}: {e}")
            
            # If no contact info parsed, add a default
            if not contact_info_dict:
                contact_info_dict[ContactChannel.SMS] = "000-000-0000"
            
            # Parse preferred channel
            pref_channel_str = patient_data.get('preferred_channel', 'sms')
            preferred_channel = ContactChannel.SMS
            if pref_channel_str == 'whatsapp':
                preferred_channel = ContactChannel.WHATSAPP
            elif pref_channel_str == 'email':
                preferred_channel = ContactChannel.EMAIL
            elif pref_channel_str == 'phone_call':
                preferred_channel = ContactChannel.PHONE_CALL
            
            # Parse last visit date
            last_visit_str = patient_data.get('last_visit_date')
            if isinstance(last_visit_str, str):
                try:
                    last_visit_date = datetime.fromisoformat(last_visit_str).date()
                except:
                    last_visit_date = datetime.strptime(last_visit_str, '%Y-%m-%d').date()
            elif isinstance(last_visit_str, date):
                last_visit_date = last_visit_str
            else:
                last_visit_date = date.today() - timedelta(days=180)
            
            patient = PatientRecord(
                patient_id=patient_id,
                name=patient_name,
                contact_info=contact_info_dict,
                preferred_channel=preferred_channel,
                last_visit_date=last_visit_date,
                treatment_type=patient_data.get('treatment_type', 'checkup'),
                recall_interval_days=patient_data.get('recall_interval_days', 180),
                no_show_history=patient_data.get('no_show_history', 0),
                language=patient_data.get('language', 'en')
            )
            
            # Add to data store
            data_store.add_patient(patient)
            
            # Add to existing sets to catch duplicates within the same upload
            existing_ids.add(patient_id)
            existing_names.add(patient_name.lower())
            
            imported_count += 1
        
        # After importing, automatically run a cycle to process new patients
        if imported_count > 0:
            print(f"\n🔄 Running agent cycle to process {imported_count} newly imported patients...")
            agent.run_daily_cycle(date.today())
        
        return jsonify({
            'success': True,
            'imported_count': imported_count,
            'skipped_count': skipped_count,
            'duplicate_patients': duplicate_patients
        })
    
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500



# ===========================================================================
# WhatsApp Alert Endpoints
# ===========================================================================

@app.route('/api/whatsapp/status')
def whatsapp_status():
    """
    Return WhatsApp channel configuration status.

    Returns:
        JSON with live/mock mode and staff number presence
    """
    return jsonify({
        'live_mode': _whatsapp_channel.is_live,
        'staff_number_configured': bool(_staff_number),
        'mode': 'live (Twilio)' if _whatsapp_channel.is_live else 'mock (demo)',
    })


@app.route('/api/whatsapp/notify-patients', methods=['POST'])
def whatsapp_notify_patients():
    """
    Judge every active follow-up case and send a WhatsApp alert to patients
    whose situation warrants one.

    Request body (optional JSON):
        {
            "patient_ids": ["P001", "P002"]   // omit to process ALL active cases
        }

    Response:
        {
            "success": true,
            "sent": 3,
            "skipped": 2,
            "failed": 0,
            "results": [
                {
                    "recipient": "Alice Tan",
                    "tier": "high",
                    "sent": true,
                    "reason": "HIGH urgency: 45 days overdue",
                    "message": "...",
                    "timestamp": "2026-09-22T..."
                },
                ...
            ]
        }
    """
    data = request.get_json(silent=True) or {}
    filter_ids = set(data.get('patient_ids', []))

    active_cases = agent.get_active_cases()

    if filter_ids:
        active_cases = [c for c in active_cases if c.patient.patient_id in filter_ids]

    if not active_cases:
        return jsonify({
            'success': True,
            'sent': 0, 'skipped': 0, 'failed': 0,
            'results': [],
            'message': 'No active cases to process.',
        })

    results = whatsapp_alert_agent.notify_active_cases(active_cases)

    sent    = sum(1 for r in results if r['sent'])
    skipped = sum(1 for r in results if not r['sent'] and r['tier'] == 'skip')
    failed  = sum(1 for r in results if not r['sent'] and r['tier'] != 'skip')

    return jsonify({
        'success': True,
        'sent': sent,
        'skipped': skipped,
        'failed': failed,
        'results': results,
    })


@app.route('/api/whatsapp/notify-escalated', methods=['POST'])
def whatsapp_notify_escalated():
    """
    Judge every escalated case and send a WhatsApp staff alert for cases
    whose priority warrants attention.

    Request body (optional JSON):
        {
            "patient_ids": ["ESC-001"]    // omit to process ALL escalated cases
        }

    Response: same shape as /api/whatsapp/notify-patients
    """
    data = request.get_json(silent=True) or {}
    filter_ids = set(data.get('patient_ids', []))

    escalations = agent.escalation_handler.get_escalated_cases()

    if filter_ids:
        escalations = [e for e in escalations if e.get('patient_id') in filter_ids]

    if not escalations:
        return jsonify({
            'success': True,
            'sent': 0, 'skipped': 0, 'failed': 0,
            'results': [],
            'message': 'No escalated cases to process.',
        })

    if not _staff_number:
        return jsonify({
            'success': False,
            'error': (
                'WHATSAPP_STAFF_NUMBER environment variable is not set. '
                'Set it to an E.164 number (e.g. +60123456789) to receive staff alerts.'
            ),
        }), 400

    results = whatsapp_alert_agent.notify_escalated_cases(escalations)

    sent    = sum(1 for r in results if r['sent'])
    skipped = sum(1 for r in results if not r['sent'] and r['tier'] == 'skip')
    failed  = sum(1 for r in results if not r['sent'] and r['tier'] != 'skip')

    return jsonify({
        'success': True,
        'sent': sent,
        'skipped': skipped,
        'failed': failed,
        'results': results,
    })


@app.route('/api/whatsapp/notify-single', methods=['POST'])
def whatsapp_notify_single():
    """
    Send a WhatsApp alert for one specific active case immediately.

    Request body:
        { "patient_id": "P001" }

    Response: single result dict (sent, tier, recipient, message, reason)
    """
    data = request.get_json(silent=True) or {}
    patient_id = data.get('patient_id')

    if not patient_id:
        return jsonify({'success': False, 'error': 'patient_id is required'}), 400

    case = agent.get_case_by_patient_id(patient_id)
    if not case:
        return jsonify({'success': False, 'error': f'No active case for patient {patient_id}'}), 404

    result = whatsapp_alert_agent.notify_case(case)
    result['success'] = True
    return jsonify(result)


if __name__ == '__main__':
    # Initialize with sample data
    from utils.sample_data import initialize_sample_data
    initialize_sample_data(data_store, calendar)
    
    port = 8080  # Using port 8080 to avoid conflicts with AirPlay Receiver
    
    print("\n" + "="*70)
    print("🏥 Patient Follow-up Agent - Web Dashboard")
    print("="*70)
    print(f"\n📊 Dashboard: http://localhost:{port}")
    print("📡 API Endpoints:")
    print("   GET  /api/status                    - Agent statistics")
    print("   GET  /api/cases                     - All active cases")
    print("   GET  /api/escalations               - Escalated cases")
    print("   POST /api/run-cycle                 - Trigger daily cycle")
    print("   POST /api/simulate-reply            - Simulate patient reply")
    print("   GET  /api/audit-logs                - View audit logs")
    print("   GET  /api/whatsapp/status           - WhatsApp channel status")
    print("   POST /api/whatsapp/notify-patients  - Send patient WhatsApp alerts")
    print("   POST /api/whatsapp/notify-escalated - Send staff escalation alerts")
    print("   POST /api/whatsapp/notify-single    - Alert one patient by ID")
    wa_mode = 'LIVE (Twilio)' if _whatsapp_channel.is_live else 'MOCK (demo/print)'
    print(f"\n📱 WhatsApp mode : {wa_mode}")
    if _staff_number:
        print(f"   Staff number  : {_staff_number}")
    else:
        print("   Staff number  : NOT SET (set WHATSAPP_STAFF_NUMBER to enable escalation alerts)")
    print("\n" + "="*70 + "\n")
    
    app.run(debug=True, host='0.0.0.0', port=port)
