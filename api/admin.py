from django.contrib import admin
from .models import CustomUser, Strategy, RevenueCatDeposit, RevenueCatConfirmation

# Register your models here.
admin.site.register(CustomUser)
admin.site.register(Strategy)
admin.site.register(RevenueCatDeposit)
admin.site.register(RevenueCatConfirmation)
