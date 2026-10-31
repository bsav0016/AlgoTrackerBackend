from django.urls import path
from api.views import (UserRegistrationView, LoginView, LogoutView, BacktestView, StrategyView, DeviceIDView, NotificationHistoryView,
                       AvailableSymbolsAndIntervalsView, PasswordResetRequestView, PasswordResetConfirmView,
                       StripePaymentView,StripePaymentConfirmationView, UserDetailsView, DeleteAccountView,
                       PromoCodeAvailabilityView, RevenueCatCustomerView, RevenueCatConfirmationView, HealthView, CreditHistoryView,
                       StrategyShareView, SharedStrategyView, SharedStrategyCloneView, StrategyActivateView, SharedStrategyCardView)
from rest_framework_simplejwt.views import TokenRefreshView


urlpatterns = [
    path('health/', HealthView.as_view(), name='health'),
    path('register/', UserRegistrationView.as_view(), name='user-registration'),
    path('promo-code-availability/', PromoCodeAvailabilityView.as_view(), name='promo-code-availability'),
    path('login/', LoginView.as_view(), name='login'),
    path('logout/', LogoutView.as_view(), name='logout'),
    path('delete-account/', DeleteAccountView.as_view(), name='delete-account'),
    path('user-details/', UserDetailsView.as_view(), name='user-details'),
    path('credits/history/', CreditHistoryView.as_view(), name='credit-history'),
    path('token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),

    path('password-reset/', PasswordResetRequestView.as_view(), name='password-reset'),
    path('password-reset-confirm/', PasswordResetConfirmView.as_view(), name='password-reset-confirm'),

    path('backtest/', BacktestView.as_view(), name='backtest'),
    path('strategy/', StrategyView.as_view(), name='strategy'),
    path('strategy/<int:pk>/', StrategyView.as_view(), name='strategy-detail'),
    path('strategy/<int:pk>/share/', StrategyShareView.as_view(), name='strategy-share'),
    path('shared/<str:share_id>/', SharedStrategyView.as_view(), name='shared-strategy'),
    path('shared/<str:share_id>/card.png', SharedStrategyCardView.as_view(), name='shared-strategy-card'),
    path('shared/<str:share_id>/clone/', SharedStrategyCloneView.as_view(), name='shared-strategy-clone'),
    path('strategy/<int:pk>/activate/', StrategyActivateView.as_view(), name='strategy-activate'),
    path('notifications/', NotificationHistoryView.as_view(), name='notifications'),
    path('device-id/', DeviceIDView.as_view(), name='device-id'),
    path('symbols-and-intervals/', AvailableSymbolsAndIntervalsView.as_view(), name='symbols-and-intervals'),
    path('payment-intent/', StripePaymentView.as_view(), name='payment-intent'),
    path('payment-confirmation/', StripePaymentConfirmationView.as_view(), name='payment-confirmation'),
    path('revenuecat-customer/', RevenueCatCustomerView.as_view(), name='revenuecat-customer'),
    path('revenuecat-confirmation/', RevenueCatConfirmationView.as_view(), name='revenuecat-confirmation'),
]
