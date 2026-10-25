"""Unit inventory, bed assignment, and persisted prescriber review requests."""
from datetime import datetime, timezone

from flask import Blueprint, g, jsonify, request
from postgrest.exceptions import APIError
from pydantic import ValidationError

from app.auth.middleware import assert_nurse_assigned, assert_patient_in_org, require_auth, require_role
from app.extensions import supabase
from app.schemas.requests import BedCreate, LocationUpdate, MedicationReviewCreate, MedicationReviewAction
from app.services.escalation import recent_vitals
from app.services.medication import sla_deadline_for

bp = Blueprint('mobile', __name__, url_prefix='/api/v1')


def validate(model):
    try:
        return model.model_validate(request.get_json(silent=True) or {}), None
    except ValidationError as exc:
        return None, (jsonify(error='Invalid request', details=exc.errors(include_context=False)), 422)


def audit(action, target_type, target_id, payload):
    supabase().table('audit_log').insert(dict(org_id=g.user.org_id, actor_id=g.user.id,
        action=action, target_type=target_type, target_id=target_id, payload=payload)).execute()


@bp.get('/beds')
@require_auth
def beds():
    user = g.user
    inventory = supabase().table('unit_beds').select('*').eq('org_id',user.org_id).execute().data or []
    # Return occupancy only. Assigned-patient identity comes from /patients/mine.
    occupied = supabase().table('patients').select('unit_id, bed').eq('org_id',user.org_id).is_('discharged_at','null').execute().data or []
    occupied_keys = {(p['unit_id'],p['bed']) for p in occupied if p.get('bed')}
    return jsonify(beds=[{**b,'occupied':(b['unit_id'],b['bed']) in occupied_keys} for b in inventory])


@bp.post('/beds')
@require_auth
@require_role('admin','charge_nurse')
def create_bed():
    body, err = validate(BedCreate)
    if err: return err
    units = supabase().table('units').select('id').eq('id',body.unit_id).eq('org_id',g.user.org_id).execute().data or []
    if not units: return jsonify(error='Unit not found'),404
    try:
        rows = supabase().table('unit_beds').insert({**body.model_dump(),'org_id':g.user.org_id}).execute().data or []
    except APIError as exc:
        if exc.code == '23505': return jsonify(error='This bed already exists in the unit'),409
        raise
    if not rows: return jsonify(error='Could not create bed'),500
    audit('bed.created','unit_bed',rows[0]['id'],body.model_dump())
    return jsonify(bed=rows[0]),201


@bp.patch('/patients/<patient_id>/location')
@require_auth
@require_role('admin','charge_nurse')
def update_location(patient_id):
    body,err=validate(LocationUpdate)
    if err: return err
    patient=assert_patient_in_org(patient_id)
    if patient.get('discharged_at'): return jsonify(error='Patient is discharged'),409
    if body.bed_id is None:
        update={'bed':None}  # Retain the existing room and unit.
    else:
        rows=supabase().table('unit_beds').select('*').eq('id',body.bed_id).eq('org_id',g.user.org_id).execute().data or []
        if not rows: return jsonify(error='Bed not found'),404
        bed=rows[0]
        update={k:bed[k] for k in ('unit_id','room','bed')}
    try:
        rows=supabase().table('patients').update(update).eq('id',patient_id).eq('org_id',g.user.org_id).is_('discharged_at','null').execute().data or []
    except APIError as exc:
        if exc.code=='23505': return jsonify(error='This bed is already occupied. Choose another bed.'),409
        raise
    if not rows: return jsonify(error='Patient is no longer active'),409
    audit('patient.location_updated','patient',patient_id,update)
    return jsonify(patient=rows[0])


@bp.get('/medication/prescribers')
@require_auth
def prescribers():
    rows=supabase().table('profiles').select('id, full_name').eq('org_id',g.user.org_id).eq('role','prescriber').execute().data or []
    return jsonify(prescribers=rows)


CONDITIONS=('blood_pressure','blood_sugar','heart_rate')


@bp.get('/medication/classes')
@require_auth
def medication_classes():
    """The vocabulary a nurse picks a suggestion from.

    Curated in the table rather than hard-coded here, and `source` rides along
    so the client can show where a class came from -- an unsourced row has not
    been clinically reviewed.
    """
    condition=request.args.get('condition')
    if condition and condition not in CONDITIONS: return jsonify(error='Unknown condition'),422
    query=supabase().table('medication_classes').select('id, condition, label, source').eq('active',True)
    if condition: query=query.eq('condition',condition)
    rows=query.order('condition').order('sort_order').execute().data or []
    return jsonify(classes=rows)


@bp.get('/medication/reviews')
@require_auth
def reviews():
    user=g.user
    query=supabase().table('medication_reviews').select(
        '*, patient:patients(display_name, room, bed), suggested_class:medication_classes(label, source)'
    ).eq('org_id',user.org_id)
    # A prescriber sees their own queue plus anything that escalated past its
    # SLA: the second level is only real if the pool can actually see the row.
    if user.role=='prescriber':
        query=query.or_(f'prescriber_id.eq.{user.id},escalated_to.eq.{user.id},status.eq.escalated')
    elif user.role=='nurse':
        assigned=supabase().table('assignments').select('patient_id').eq('org_id',user.org_id).eq('nurse_id',user.id).is_('shift_end','null').execute().data or []
        ids=[a['patient_id'] for a in assigned]
        if not ids: return jsonify(reviews=[])
        query=query.in_('patient_id',ids)
    return jsonify(reviews=query.order('created_at',desc=True).limit(200).execute().data or [])


@bp.post('/medication/reviews')
@require_auth
@require_role('nurse','charge_nurse','admin')
def create_review():
    body,err=validate(MedicationReviewCreate)
    if err: return err
    patient=assert_patient_in_org(body.patient_id)
    assert_nurse_assigned(body.patient_id)
    if patient.get('discharged_at'): return jsonify(error='Patient is discharged'),409
    recipient=supabase().table('profiles').select('id').eq('id',body.prescriber_id).eq('org_id',g.user.org_id).eq('role','prescriber').execute().data or []
    if not recipient: return jsonify(error='Prescriber not found in this organization'),404
    if body.suggested_class_id:
        klass=supabase().table('medication_classes').select('id, condition').eq('id',body.suggested_class_id).eq('active',True).execute().data or []
        if not klass: return jsonify(error='Medication class not found'),404
        # A beta blocker filed under blood sugar would reach a prescriber as a
        # nonsense suggestion, so the pairing is checked rather than trusted.
        if klass[0]['condition']!=body.condition:
            return jsonify(error='Medication class does not match the requested condition'),422
    vitals=recent_vitals(body.patient_id,limit=1)
    rows=supabase().table('medication_reviews').insert({**body.model_dump(),'org_id':g.user.org_id,
        'requested_by':g.user.id,'vitals_snapshot':vitals[0] if vitals else {},
        'sla_deadline':sla_deadline_for(g.user.org_id)}).execute().data or []
    if not rows: return jsonify(error='Could not send review request'),500
    audit('medication_review.requested','medication_review',rows[0]['id'],{'patient_id':body.patient_id,'prescriber_id':body.prescriber_id})
    return jsonify(review=rows[0]),201


@bp.patch('/medication/reviews/<review_id>')
@require_auth
def act_review(review_id):
    body,err=validate(MedicationReviewAction)
    if err: return err
    user=g.user
    rows=supabase().table('medication_reviews').select('*').eq('id',review_id).eq('org_id',user.org_id).execute().data or []
    if not rows: return jsonify(error='Review request not found'),404
    row=rows[0]
    DECISIONS={'approve':'approved','reject':'rejected','review':'reviewed'}
    if user.role=='prescriber':
        # Once escalated the request belongs to the pool, so any prescriber in
        # the org can answer it. Before that it is one named person's job.
        if row['status']!='escalated' and row['prescriber_id']!=user.id:
            return jsonify(error='This request is assigned to another prescriber'),403
    else:
        assert_nurse_assigned(row['patient_id'])
        if body.action!='dismiss': return jsonify(error='Only a prescriber can decide this request'),403
    # Both open states are actionable; an escalated request is still waiting.
    if row['status'] not in ('pending','escalated'): return jsonify(error='This request is already closed'),409
    now=datetime.now(timezone.utc).isoformat()

    if body.action=='request_info':
        # Stays open and keeps its deadline: the nurse may not reply, and an
        # unanswered request must still reach a second prescriber on time.
        update={'info_requested_at':now,'info_requested_by':user.id,'info_request_note':body.note}
    elif body.action=='acknowledge':
        # Deliberately does NOT stop the escalation clock. Acknowledging is "I
        # have seen this", not an answer, and a request that was seen and then
        # left undecided is precisely the failure the second level exists to
        # catch. The row keeps its open status and still escalates on time.
        if row.get('acknowledged_at'): return jsonify(error='This request is already acknowledged'),409
        update={'acknowledged_at':now,'acknowledged_by':user.id}
    else:
        update={'status':DECISIONS.get(body.action,'dismissed'),'decision_note':body.note,
            'decided_at':now,'decided_by':user.id,
            # reviewed_at/reviewed_by predate the approval states; keep writing
            # them so the dashboard's existing queries keep resolving.
            'review_note':body.note,'reviewed_at':now,'reviewed_by':user.id}

    if body.prescriptions is not None:
        update['prescriptions'] = [item.model_dump() for item in body.prescriptions]
        update['reviewed_sections'] = body.reviewed_sections

    changed=supabase().table('medication_reviews').update(update).eq('id',review_id).eq(
        'org_id',user.org_id).in_('status',['pending','escalated']).execute().data or []
    if not changed: return jsonify(error='This request was already changed. Refresh the queue.'),409
    audit('medication_review.'+body.action,'medication_review',review_id,
        {'note':body.note,'was_escalated':row['status']=='escalated'})
    return jsonify(review=changed[0])
