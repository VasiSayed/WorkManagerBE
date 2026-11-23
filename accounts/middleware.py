from django.http import JsonResponse
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.authentication import JWTAuthentication

from .modules import module_for_path, user_modules


class ModuleAccessMiddleware:
    """Return 403 for the API of a module the signed-in user has not been given.

    JWT auth normally runs inside DRF views, so this authenticates the request itself for the
    enforced paths only. Unauthenticated requests pass through so the view still answers 401.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self.jwt = JWTAuthentication()

    def __call__(self, request):
        module = module_for_path(request.path)
        if module and request.method != "OPTIONS":
            try:
                result = self.jwt.authenticate(request)
            except AuthenticationFailed:
                result = None
            if result is not None:
                user = result[0]
                if module not in user_modules(user):
                    return JsonResponse(
                        {"detail": "You don't have access to this module.", "code": "module_not_granted", "module": module},
                        status=403,
                    )
        return self.get_response(request)
