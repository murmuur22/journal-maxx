from .models import User


def get_reviewer_patient(request):
    if hasattr(request, "_reviewer_patient"):
        return request._reviewer_patient
    selected = None
    if request.user.is_authenticated and request.user.role == User.Role.REVIEWER:
        patients = User.objects.filter(role=User.Role.PATIENT, is_active=True, therapist_assignments__therapist=request.user).order_by("username")
        selected_id = request.session.get("review_patient_id")
        selected = patients.filter(id=selected_id).first() if selected_id else patients.first()
        if selected and selected_id != selected.id:
            request.session["review_patient_id"] = selected.id
        request._reviewer_patients = patients
    request._reviewer_patient = selected
    return selected


def reviewer_patient(request):
    selected = get_reviewer_patient(request)
    patients = getattr(request, "_reviewer_patients", User.objects.none())
    return {"review_patient": selected, "review_patient_options": patients}
