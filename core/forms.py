from django import forms

class AddendumForm(forms.Form):
    body = forms.CharField(widget=forms.Textarea(attrs={"rows": 5, "placeholder": "Add something without changing the original…"}))

class ReviewerStatusForm(forms.Form):
    is_read = forms.BooleanField(required=False, label="Reviewed")

class TherapistCommentForm(forms.Form):
    body = forms.CharField(label="Comment", widget=forms.Textarea(attrs={"rows": 5, "placeholder": "Write a comment the patient can read…"}))
