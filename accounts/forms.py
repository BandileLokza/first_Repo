from django import forms
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password

class LoginForm(forms.Form):
    identifier = forms.CharField(
        required=True,
        label="",
        widget=forms.TextInput(attrs={
            "placeholder": "Email or Username",
            "class": "form-control",
            "autofocus": "autofocus",
        }),
    )
    password = forms.CharField(
        required=True,
        label="",
        widget=forms.PasswordInput(attrs={
            "placeholder": "Password",
            "class": "form-control",
        }),
    )
class SignUpForm(forms.Form):
    username = forms.CharField(
        required=False,
        max_length=150,
        label="",
        widget=forms.TextInput(attrs={
            "placeholder": "Choose a username",
            "class": "form-control",
        }),
    )

    email = forms.EmailField(
        required=False,
        label="",
        widget=forms.EmailInput(attrs={
            "placeholder": "Email address (optional)",
            "class": "form-control",
        }),
    )

    password1 = forms.CharField(
        label="",
        widget=forms.PasswordInput(attrs={
            "placeholder": "Create a password",
            "class": "form-control",
        }),
    )

    password2 = forms.CharField(
        label="",
        widget=forms.PasswordInput(attrs={
            "placeholder": "Confirm your password",
            "class": "form-control",
        }),
    )
    def clean(self):
        cleaned_data = super().clean()

        username = cleaned_data.get("username")
        email = cleaned_data.get("email")
        password1 = cleaned_data.get("password1")
        password2 = cleaned_data.get("password2")

        if not username and not email:
            raise forms.ValidationError(
                "Please provide either a username or an email."
            )

        if username:
            if User.objects.filter(username=username).exists():
                self.add_error(
                    "username",
                    "That username is already taken."
                )

        if email:
            if User.objects.filter(email=email).exists():
                self.add_error(
                    "email",
                    "An account with that email already exists."
                )

        if password1 != password2:
            self.add_error(
                "password2",
                "Passwords do not match."
            )

        if password1:
            validate_password(password1)

        return cleaned_data