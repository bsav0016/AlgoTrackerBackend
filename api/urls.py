from django.urls import path
from api.views import UserRegistrationView, LoginView, LogoutView, BacktestView, StrategyView
from rest_framework_simplejwt.views import TokenRefreshView


urlpatterns = [
    path('register/', UserRegistrationView.as_view(), name='user-registration'),
    path('login/', LoginView.as_view(), name='login'),
    path('logout/', LogoutView.as_view(), name='logout'),
    path('token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    path('backtest/', BacktestView.as_view(), name='backtest'),
    path('strategy/', StrategyView.as_view(), name='strategy')
]
