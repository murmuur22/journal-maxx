from django import forms

class MultiFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True

class DiaryForm(forms.Form):
    activities = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 4, "placeholder": "What did you do today?"}))
    journal = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 7, "placeholder": "Write as much or as little as you want…"}))
    attachments = forms.FileField(required=False, widget=MultiFileInput())

class AddendumForm(forms.Form):
    body = forms.CharField(widget=forms.Textarea(attrs={"rows": 5, "placeholder": "Add something without changing the original…"}))

class ReviewerForm(forms.Form):
    is_read = forms.BooleanField(required=False)
    starred = forms.BooleanField(required=False)
    tags = forms.CharField(required=False, help_text="Comma-separated")
    collections = forms.CharField(required=False, help_text="Comma-separated")
    private_note = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 4}))
