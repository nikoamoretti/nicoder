"""Named profiles: frozen identities, fallback selection, no auto-switching."""
import pytest

from local_coder import profiles
from local_coder.cli import _effective_channel, _effective_protocol, _effective_thinking, _effective_verification
from local_coder.inference.grammar import constraint_id_v2, constraint_id_v3
from local_coder.inference.llama_cpp import TRANSPORT_POLICY

from .conftest import make_controller


def test_d2_identity_is_frozen():
    profile = profiles.resolve_profile("d2")
    assert profile.protocol == "3"
    assert profile.constraint == constraint_id_v3()
    assert profile.catalogue == "span-catalogue-v2"
    assert profile.verification_policy == "mandatory"
    assert profile.channel_policy == "final-only"
    assert profile.thinking == "off"


def test_d1_identity_is_frozen_fallback():
    profile = profiles.resolve_profile("d1")
    assert profile.protocol == "2"
    assert profile.constraint == constraint_id_v2()
    assert profile.catalogue == "none"
    assert profile.verification_policy == "mandatory"


def test_unknown_profile_is_refused():
    with pytest.raises(ValueError):
        profiles.resolve_profile("d9")


def test_profile_identity_records_transport_and_catalogue():
    identity = profiles.profile_identity(profiles.resolve_profile("d2"))
    assert identity["profile_name"] == "d2"
    assert identity["catalogue"] == "span-catalogue-v2"
    assert identity["transport_policy"] == TRANSPORT_POLICY
    assert set(identity) >= {"protocol_version", "constraint", "thinking"}


def test_effective_values_default_to_profile_and_allow_explicit_override():
    from types import SimpleNamespace

    args = SimpleNamespace(protocol=None, verification_policy=None, channel=None, thinking=None)
    profile = profiles.resolve_profile("d2")
    assert _effective_protocol(args, profile) == "3"
    assert _effective_verification(args, profile) == "mandatory"
    assert _effective_channel(args, profile) == "final-only"
    assert _effective_thinking(args, profile) == "off"
    override = SimpleNamespace(protocol="1", verification_policy="off", channel="legacy", thinking="auto")
    assert _effective_protocol(override, profile) == "1"
    assert _effective_verification(override, profile) == "off"


def test_controller_records_profile_identity(repo_copy, state_dir):
    profile = profiles.resolve_profile("d2")
    identity = profiles.profile_identity(profile)
    controller, _ = make_controller(repo_copy, state_dir, [], profile_identity=identity,
                                    protocol_version=profile.protocol,
                                    verification_policy=profile.verification_policy)
    controller.start()
    assert controller.state.provenance["profile_name"] == "d2"
    assert controller.state.provenance["constraint"] == identity["constraint"]
    assert controller.state.provenance["verification_policy"] == "mandatory"
    assert controller.state.provenance["protocol_version"] == "3"