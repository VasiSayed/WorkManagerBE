from django.contrib.auth import get_user_model
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from .models import UserModuleAccess
from .modules import MODULES, is_admin
from .serializers import (
    ChangePasswordSerializer,
    ModuleListSerializer,
    UserAccessSerializer,
    ForgotPasswordSerializer,
    RegisterSerializer,
    ResetPasswordSerializer,
    UserSerializer,
)

User = get_user_model()


class RegisterAPIView(generics.CreateAPIView):
    queryset = User.objects.all()
    serializer_class = RegisterSerializer
    permission_classes = [permissions.AllowAny]


class MeAPIView(generics.RetrieveUpdateAPIView):
    serializer_class = UserSerializer

    def get_object(self):
        return self.request.user


class ChangePasswordAPIView(APIView):
    def post(self, request):
        serializer = ChangePasswordSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response({"detail": "Password changed successfully."})


class ForgotPasswordAPIView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = ForgotPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        # Hook your email provider here. For now this is a safe placeholder response.
        return Response({"detail": "If this email exists, reset instructions will be sent."})


class ResetPasswordAPIView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = ResetPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        # Token verification can be connected later with Django PasswordResetTokenGenerator.
        return Response({"detail": "Reset token endpoint is ready. Connect token verification before production."}, status=status.HTTP_202_ACCEPTED)


LoginAPIView = TokenObtainPairView
RefreshTokenAPIView = TokenRefreshView


class IsAppAdmin(permissions.BasePermission):
    def has_permission(self, request, view):
        return is_admin(request.user)


class UserAccessListAPIView(generics.ListAPIView):
    """Admins: every user with their modules, plus the list of modules."""

    serializer_class = UserAccessSerializer
    permission_classes = [IsAppAdmin]
    pagination_class = None

    def get_queryset(self):
        qs = User.objects.prefetch_related("module_access").order_by("full_name", "email")
        search = self.request.query_params.get("search", "").strip()
        if search:
            from django.db.models import Q

            qs = qs.filter(Q(email__icontains=search) | Q(full_name__icontains=search))
        return qs

    def list(self, request, *args, **kwargs):
        users = UserAccessSerializer(self.get_queryset(), many=True).data
        return Response({"modules": [{"key": k, "label": label} for k, label in MODULES], "users": users})


class UserModulesAPIView(APIView):
    """Admins: replace the set of modules a user can use."""

    permission_classes = [IsAppAdmin]

    def put(self, request, user_id):
        user = User.objects.filter(pk=user_id).first()
        if not user:
            return Response({"detail": "User not found."}, status=status.HTTP_404_NOT_FOUND)
        data = ModuleListSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        wanted = set(data.validated_data["modules"])
        UserModuleAccess.objects.filter(user=user).exclude(module__in=wanted).delete()
        UserModuleAccess.objects.bulk_create(
            [UserModuleAccess(user=user, module=m) for m in wanted], ignore_conflicts=True
        )
        return Response(UserAccessSerializer(User.objects.get(pk=user.pk)).data)
