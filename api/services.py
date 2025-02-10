from rest_framework_simplejwt.tokens import RefreshToken


class AuthService:
    def create_tokens(self, user):
        refresh = RefreshToken.for_user(user)
        return {
            "access": str(refresh.access_token),
            "refresh": str(refresh),
        }
