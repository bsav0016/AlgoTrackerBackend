from django.contrib import admin
from .models import CustomUser, Strategy, RevenueCatCustomer, RevenueCatConfirmation

# Register your models here.
admin.site.register(CustomUser)
admin.site.register(Strategy)
admin.site.register(RevenueCatCustomer)
admin.site.register(RevenueCatConfirmation)
