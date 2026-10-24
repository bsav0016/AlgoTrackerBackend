from django.contrib import admin
from .models import CreditTransaction, CustomUser, Strategy, RevenueCatCustomer, RevenueCatConfirmation

# Register your models here.
admin.site.register(CustomUser)
admin.site.register(Strategy)
admin.site.register(RevenueCatCustomer)
admin.site.register(RevenueCatConfirmation)


@admin.register(CreditTransaction)
class CreditTransactionAdmin(admin.ModelAdmin):
    """Read-only: the ledger is only ever written by `api.credits.apply`."""
    list_display = ("created_at", "user", "bucket", "amount", "balance_after", "kind", "idempotency_key")
    list_filter = ("kind", "bucket")
    search_fields = ("user__username", "idempotency_key")
    ordering = ("-id",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
