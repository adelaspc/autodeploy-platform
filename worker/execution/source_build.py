from worker.services.image_build import ImageBuildServiceMixin
from worker.services.registry import RegistryServiceMixin
from worker.services.repository import RepositoryServiceMixin


class SourceBuildMixin(RepositoryServiceMixin, ImageBuildServiceMixin, RegistryServiceMixin):
    """Compatibility facade for executor source, build, test, and registry capabilities."""
