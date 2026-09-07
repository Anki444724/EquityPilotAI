"""The document store must be one volume, mounted in both containers.

Regression suite for a production fault in which a `DocumentJob` failed all
three attempts because the object it was claimed for did not exist in the
worker's filesystem. The deployment set::

    DOCUMENT_STORAGE_BACKEND=local
    DOCUMENT_STORAGE_PATH=/data/documents

but mounted nothing at `/data/documents` in either container. The API then
streamed each upload into its own ephemeral container filesystem and the
worker — the process that claims `DocumentJob` rows and re-reads the stored
source — looked for those bytes in a *different* ephemeral filesystem. Upload
and ingestion were two islands sharing a database: the document row said
`stored`, the worker said `storage object not found`, and no amount of retries
could reconcile them. Nothing about the job logic, the claim or the pipeline
was wrong; the hand-off underneath them was.

The fix is deployment topology, so these tests read the deployment: they parse
`docker-compose.yml` (and, for the single-container/Railway shape, the
Dockerfile, the entrypoint and the application defaults) and assert the
properties that make the hand-off work:

* one NAMED volume `documents`, mounted at `/data/documents` by both `api`
  and `worker` — and by nothing else;
* both services resolve the storage backend and path explicitly, and the
  configured path is the mount point;
* the `backups` volume and the rest of the topology are untouched;
* the single-container/Railway shape, which depends on image and config
  defaults rather than compose, still points every layer at the same path.

Named volumes survive `compose restart`, container recreation and image
rebuilds; only `compose down -v` removes them. That property — persistence
independent of container lifecycle — is exactly what a bind mount to a
container-local path or an anonymous volume would not give.

PyYAML is a dependency of `uvicorn[standard]` (pinned in requirements.txt), so
parsing the compose file adds no new requirement.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from app.core.config import settings

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
DOCKERFILE = REPO_ROOT / "backend" / "Dockerfile"
ENTRYPOINT = REPO_ROOT / "backend" / "docker-entrypoint.sh"

STORAGE_VOLUME = "documents"
STORAGE_PATH = "/data/documents"
BACKUP_MOUNT = "backups:/app/backups"


def _compose() -> dict:
    data = yaml.safe_load(COMPOSE_FILE.read_text())
    assert isinstance(data, dict), "docker-compose.yml must parse to a mapping"
    return data


def _mounts(service: dict) -> list[tuple[str | None, str]]:
    """A service's volumes as (source, target) pairs."""
    pairs: list[tuple[str | None, str]] = []
    for mount in service.get("volumes") or []:
        if isinstance(mount, str):
            parts = mount.split(":")
            source, target = parts[0], parts[1]
        else:  # long syntax
            source, target = mount.get("source"), mount["target"]
        pairs.append((source, target))
    return pairs


def _by_target(service: dict) -> dict[str, str | None]:
    """A service's volumes as {container path: source}."""
    return {target: source for source, target in _mounts(service)}


# ===========================================================================
# The shared volume
# ===========================================================================
class TestOneNamedVolumeSharedByBothServices:
    def test_the_documents_volume_is_declared(self):
        """A named top-level volume `documents` must exist.

        Named — not external, not a bind mount — because only a
        compose-managed named volume is created automatically, reused across
        recreations and rebuilds, and survives until explicitly deleted.
        """
        volumes = _compose().get("volumes") or {}
        assert STORAGE_VOLUME in volumes, (
            f"top-level volumes must declare a named `{STORAGE_VOLUME}` "
            f"volume; found: {sorted(volumes)}"
        )
        declaration = volumes[STORAGE_VOLUME] or {}
        assert not declaration.get("external"), (
            f"`{STORAGE_VOLUME}` must not be external: the stack has to be "
            "bring-up-able without a pre-created volume"
        )

    def test_the_api_mounts_it_at_the_storage_path(self):
        api = _compose()["services"]["api"]
        assert (STORAGE_VOLUME, STORAGE_PATH) in _mounts(api), (
            f"api must mount the named volume `{STORAGE_VOLUME}` at "
            f"{STORAGE_PATH}; mounts: {_mounts(api)}"
        )

    def test_the_worker_mounts_it_at_the_storage_path(self):
        worker = _compose()["services"]["worker"]
        assert (STORAGE_VOLUME, STORAGE_PATH) in _mounts(worker), (
            f"worker must mount the named volume `{STORAGE_VOLUME}` at "
            f"{STORAGE_PATH}; mounts: {_mounts(worker)}"
        )

    def test_both_services_see_the_same_volume_at_the_same_path(self):
        """The hand-off property: same source, same target, both services.

        Two mounts of two different volumes at the same path would still be
        two islands; the volume NAME is what makes it one store.
        """
        compose = _compose()
        api = _by_target(compose["services"]["api"])
        worker = _by_target(compose["services"]["worker"])
        assert api.get(STORAGE_PATH) == STORAGE_VOLUME
        assert worker.get(STORAGE_PATH) == STORAGE_VOLUME
        assert api[STORAGE_PATH] == worker[STORAGE_PATH], (
            "api and worker must share ONE volume at "
            f"{STORAGE_PATH}, not two volumes at the same path"
        )

    def test_no_other_service_mounts_it(self):
        """postgres, redis and web have no business in the document store."""
        compose = _compose()
        mounted_by = {
            name for name, service in compose["services"].items()
            if STORAGE_VOLUME in dict(_mounts(service))
        }
        assert mounted_by == {"api", "worker"}, (
            f"only api and worker may mount `{STORAGE_VOLUME}`; found: "
            f"{sorted(mounted_by)}"
        )

    def test_the_mount_is_a_named_volume_not_a_host_path(self):
        """A bind mount would tie the store to one host directory and break
        the volume-per-stack lifecycle the persistence argument rests on."""
        for name in ("api", "worker"):
            source = _by_target(_compose()["services"][name])[STORAGE_PATH]
            assert source == STORAGE_VOLUME, (
                f"{name}'s {STORAGE_PATH} mount must source the named "
                f"volume `{STORAGE_VOLUME}`, got {source!r}"
            )


# ===========================================================================
# Explicit configuration on both services
# ===========================================================================
class TestStorageConfigurationIsExplicitOnBothServices:
    def test_backend_is_local_on_both_services(self):
        compose = _compose()
        for name in ("api", "worker"):
            backend = compose["services"][name]["environment"].get(
                "DOCUMENT_STORAGE_BACKEND"
            )
            assert backend == "local", (
                f"{name} must set DOCUMENT_STORAGE_BACKEND=local explicitly "
                "(the deployment fault was a configuration that nobody "
                f"could see in the file); got {backend!r}"
            )

    def test_path_is_set_on_both_services_and_matches_the_mount(self):
        compose = _compose()
        for name in ("api", "worker"):
            service = compose["services"][name]
            path = service["environment"].get("DOCUMENT_STORAGE_PATH")
            assert path == STORAGE_PATH, (
                f"{name} must set DOCUMENT_STORAGE_PATH={STORAGE_PATH} "
                f"explicitly; got {path!r}"
            )
            # A configured path that is not the mount point would send the
            # bytes to the ephemeral container filesystem again.
            assert STORAGE_PATH in _by_target(service), (
                f"{name} configures DOCUMENT_STORAGE_PATH={STORAGE_PATH} "
                "but mounts nothing there"
            )


# ===========================================================================
# The rest of the topology is untouched
# ===========================================================================
class TestTheRestOfTheTopologyIsUntouched:
    def test_backups_volume_is_still_mounted_by_both_services(self):
        compose = _compose()
        for name in ("api", "worker"):
            assert BACKUP_MOUNT in (compose["services"][name]["volumes"]), (
                f"{name} must keep its {BACKUP_MOUNT} mount"
            )

    def test_postgres_redis_and_web_are_unchanged(self):
        volumes = _compose().get("volumes") or {}
        for expected in ("postgres_data", "redis_data", "backups"):
            assert expected in volumes, f"volume `{expected}` went missing"
        services = _compose()["services"]
        assert {"postgres", "redis", "api", "worker", "web"} == set(services)
        assert ("postgres_data", "/var/lib/postgresql/data") in _mounts(
            services["postgres"]
        )
        assert ("redis_data", "/data") in _mounts(services["redis"])

    def test_both_long_running_services_restart_unless_stopped(self):
        """A restart re-attaches the same named volume; that only helps if
        the orchestrator actually restarts the containers."""
        compose = _compose()
        for name in ("api", "worker"):
            assert compose["services"][name].get("restart") == "unless-stopped"

    def test_the_worker_healthcheck_is_still_disabled(self):
        """The image HEALTHCHECK curls /health/ready on :8000, which the
        worker process does not serve; a disabled check here is load-bearing
        for the split shape and must survive this change."""
        worker = _compose()["services"]["worker"]
        assert worker.get("healthcheck", {}).get("disable") is True


# ===========================================================================
# The single-container / Railway shape is not broken
# ===========================================================================
class TestTheSingleContainerShapeStillPointsAtTheSamePath:
    """Railway runs ONE container from this image with no compose file at
    all: it relies on the image's own directories, the entrypoint's
    ownership repair and the application settings defaults. Those layers must
    keep agreeing with the compose path, or the two deployment shapes drift
    apart.

    No application code is imported beyond `settings` — this suite pins the
    deployment contract, it does not exercise the pipeline.
    """

    def test_the_application_default_path_is_the_compose_path(self):
        assert settings.DOCUMENT_STORAGE_PATH == STORAGE_PATH
        assert settings.DOCUMENT_STORAGE_BACKEND == "local"

    def test_the_dockerfile_still_prepares_the_mount_point(self):
        dockerfile = DOCKERFILE.read_text()
        assert "/data/documents" in dockerfile, (
            "the image must still create /data/documents: a volume mounted "
            "over a path the image does not own leaves the app unable to "
            "write a byte"
        )
        assert "docker-entrypoint.sh" in dockerfile, (
            "the entrypoint (which takes ownership of the mount and drops "
            "privileges) must stay wired in"
        )

    def test_the_entrypoint_default_is_the_compose_path(self):
        entrypoint = ENTRYPOINT.read_text()
        assert 'DOCUMENT_STORAGE_PATH:-/data/documents' in entrypoint, (
            "the entrypoint must keep defaulting STORAGE_PATH to "
            f"{STORAGE_PATH} so the single-container shape repairs the "
            "same directory compose mounts the volume at"
        )

    def test_compose_env_overrides_agree_with_the_defaults(self):
        """The compose settings are a restatement of the defaults, not a
        redefinition: if they ever diverge, the two shapes disagree about
        where documents live and this suite must fail loudly."""
        compose = _compose()
        for name in ("api", "worker"):
            env = compose["services"][name]["environment"]
            assert env["DOCUMENT_STORAGE_PATH"] == settings.DOCUMENT_STORAGE_PATH
            assert env["DOCUMENT_STORAGE_BACKEND"] == "local"
