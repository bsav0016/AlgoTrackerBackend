from django.contrib import admin
from .models import CustomUser, Strategy, StockValue

# Register your models here.
admin.site.register(CustomUser)
admin.site.register(Strategy)
admin.site.register(StockValue)
