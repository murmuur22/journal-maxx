from django import forms
from .models import AttachmentSettings


class AttachmentLimitsForm(forms.ModelForm):
    class Meta:
        model = AttachmentSettings
        fields = ["file_limit_mib", "card_limit_mib"]
        labels = {"file_limit_mib": "Per file (MiB)", "card_limit_mib": "Per card (MiB)"}
        widgets = {name: forms.NumberInput(attrs={"min": 1, "step": 1}) for name in fields}

    def clean(self):
        values = super().clean()
        file_limit, card_limit = values.get("file_limit_mib"), values.get("card_limit_mib")
        if file_limit is not None and file_limit < 1:
            self.add_error("file_limit_mib", "Enter at least 1 MiB.")
        if card_limit is not None and card_limit < 1:
            self.add_error("card_limit_mib", "Enter at least 1 MiB.")
        if file_limit and card_limit and file_limit > card_limit:
            self.add_error("card_limit_mib", "The card limit must be at least the file limit.")
        return values

class AddendumForm(forms.Form):
    body = forms.CharField(widget=forms.Textarea(attrs={"rows": 5, "placeholder": "Add something without changing the original…"}))

class ReviewerStatusForm(forms.Form):
    is_read = forms.BooleanField(required=False, label="Reviewed")

class TherapistCommentForm(forms.Form):
    body = forms.CharField(label="Comment", widget=forms.Textarea(attrs={"rows": 5, "placeholder": "Write a comment the patient can read…"}))
