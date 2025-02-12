from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView
from django.core.exceptions import ObjectDoesNotExist
from rest_framework_simplejwt.token_blacklist.models import OutstandingToken, BlacklistedToken
from rest_framework.permissions import AllowAny, IsAuthenticated
from .serializers import UserRegistrationSerializer, LoginSerializer, CustomUserSerializer
from .services import AuthService

class UserRegistrationView(APIView):
    def post(self, request, format=None):
        try:
            serializer = UserRegistrationSerializer(data=request.data)
            if serializer.is_valid():
                user = serializer.save()
                tokens = AuthService.create_tokens(user=user)

                user_data = CustomUserSerializer(user).data
                return Response({
                        **user_data,
                        "refresh_token": tokens['refresh'],
                        "access_token": tokens['access'],
                    },
                    status=status.HTTP_201_CREATED
                )
            return Response({"detail": serializer.errors}, status=status.HTTP_409_CONFLICT)
        except Exception as e:
            print(e)
            return Response({"detail": "Error registering user"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class LoginView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny, ]

    def post(self, request, format=None):
        serializer = LoginSerializer(data=request.data)
        if serializer.is_valid():
            user = serializer.validate(data=request.data)
            tokens = AuthService.create_tokens(user=user)
            user_data = CustomUserSerializer(user).data

            return Response({
                    **user_data,
                    "refresh_token": tokens['refresh'],
                    "access_token": tokens['access'],
                },
                status=status.HTTP_200_OK
            )
        return Response({"detail": "Invalid credentials."}, status=status.HTTP_400_BAD_REQUEST)


class LogoutView(APIView):
    def post(self, request, *args, **kwargs):
        try:
            if self.request.data.get('all'):
                token: OutstandingToken
                for token in OutstandingToken.objects.filter(user=request.user):
                    _, _ = BlacklistedToken.objects.get_or_create(token=token)
                return Response({"message": "All refresh tokens blacklisted"})
            refresh_token = request.data.get('refresh')
            outstanding_token = OutstandingToken.objects.get(token=refresh_token)
            BlacklistedToken.objects.get_or_create(token=outstanding_token)
            return Response({"message": "Logout completed"}, status=status.HTTP_200_OK)

        except ObjectDoesNotExist:
            return Response({"detail": "Token does not exist"}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            # Return the error response correctly
            return Response({"detail": "Error: " + str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
