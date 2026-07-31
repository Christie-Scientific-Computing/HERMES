from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.staticfiles.urls import staticfiles_urlpatterns
from django.urls import include, path

urlpatterns = [
    path('admin/', admin.site.urls),
    path('accounts/', include('accounts.urls')),
    path('projects/', include('research_projects.urls')),
    path('', include('jobs.urls')),
]

if settings.DEBUG:
    # runserver auto-serves STATIC_URL as a management-command convenience;
    # uvicorn/daphne (what this project actually runs under) don't, so it
    # has to be wired up explicitly or every {% static %} 404s.
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += staticfiles_urlpatterns()
