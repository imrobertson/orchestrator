#!/usr/bin/env python3
"""
Verification harness for RecipeConfig.entrypoint (schema 2 -> 3).

Follows the verify_*.py pattern: stub out `common` so recipes.py can be
imported standalone without the rest of the package, then exercise the
specific invariants this change rests on.

The load-bearing claim is that "" and None are DIFFERENT values all the
way through -- pydantic, the hash payload, and the catalog response. If
"" ever collapses to None (or to falsy-therefore-absent), a recipe that
neutralizes its image ENTRYPOINT silently reverts to not neutralizing it,
which is exactly the failure this field exists to fix.
"""
SCOPE_NOTE = """
SCOPE -- read before trusting a pass:

  VERIFIED here: build_config_payload() / compute_config_hash() treat
  None, "" and a real value as three distinct inputs, and the payload
  shape is what schema 3 claims. That logic is plain Python.

  NOT VERIFIED here: pydantic v2's own handling of Optional[str] = None
  when given "". This sandbox has no network and pydantic cannot be
  installed, so BaseModel below is a STUB that simply assigns what it is
  given. Section [2] therefore proves nothing about real pydantic --
  re-run this harness on maestro (where pydantic 2.13.5 is present) with
  the stub removed to actually confirm it.
"""

import sys
import types
from pathlib import Path

from _repo import add_common_to_path
BASE_DIR = add_common_to_path()

# --- stub pydantic (sandbox has no network; see SCOPE_NOTE) ---------------
try:
    import pydantic  # noqa: F401
    PYDANTIC_IS_REAL = True
except ModuleNotFoundError:
    PYDANTIC_IS_REAL = False
    pyd = types.ModuleType("pydantic")

    class _Field:
        def __init__(self, default=None, default_factory=None, **kw):
            self.default = default
            self.default_factory = default_factory

    def Field(default=None, default_factory=None, **kw):  # noqa: N802
        return _Field(default, default_factory, **kw)

    class BaseModel:
        def __init__(self, **data):
            ann = {}
            for klass in reversed(type(self).__mro__):
                ann.update(getattr(klass, "__annotations__", {}))
            for name in ann:
                if name in data:
                    setattr(self, name, data[name])
                    continue
                default = getattr(type(self), name, None)
                if isinstance(default, _Field):
                    default = (default.default_factory()
                               if default.default_factory else default.default)
                setattr(self, name, default)
            for name, validator in getattr(type(self), "_stub_validators", {}).items():
                if hasattr(self, name):
                    setattr(self, name, validator(type(self), getattr(self, name)))

        def model_dump(self, include=None):
            ann = {}
            for klass in reversed(type(self).__mro__):
                ann.update(getattr(klass, "__annotations__", {}))
            return {k: getattr(self, k) for k in ann
                    if include is None or k in include}

    def field_validator(name, mode="after"):  # noqa: N802
        def wrap(fn):
            inner = fn.__func__ if isinstance(fn, classmethod) else fn

            class _Registrar(classmethod):
                pass
            fn._stub_field = name
            return classmethod(inner)
        return wrap

    class ValidationError(Exception):
        pass

    pyd.BaseModel = BaseModel
    pyd.Field = Field
    pyd.field_validator = field_validator
    pyd.ValidationError = ValidationError
    sys.modules["pydantic"] = pyd

# --- stub `common` package so recipes.py imports standalone ---------------
common = types.ModuleType("common")
common.__path__ = []
sys.modules.setdefault("common", common)

config_stub = types.ModuleType("common.config")


class _ClusterCfg:
    global_hf_hub_offline = 0
    global_transformers_offline = 0
    default_image = "stub/default:latest"


config_stub.load_cluster_config = lambda: _ClusterCfg()
config_stub.BASE_DIR = BASE_DIR
sys.modules["common.config"] = config_stub

import importlib

recipes = importlib.import_module("recipes")

print(SCOPE_NOTE)
print(f"pydantic: {'REAL' if PYDANTIC_IS_REAL else 'STUBBED'}")

FAILURES = []


def check(label, condition, detail=""):
    if condition:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}  {detail}")
        FAILURES.append(label)


def make(**overrides):
    base = dict(
        recipe_version="1",
        hf_path="org/model",
        image="img:tag",
        gpu_util=0.8,
        # Built explicitly: the pydantic stub does not coerce nested dicts
        # into sub-models the way real pydantic does. Real pydantic accepts
        # either form, so this stays correct when run unstubbed on maestro.
        topologies={
            "2_node": recipes.TopologyConfig(
                max_model_len=1024, tp_size=2, pp_size=1,
                env_vars=["A=1"], vllm_args="--trust-remote-code",
            )
        },
    )
    base.update(overrides)
    return recipes.RecipeConfig(**base)


print("\n[1] schema version bumped")
check("_CONFIG_HASH_SCHEMA == 5", recipes._CONFIG_HASH_SCHEMA == 5,
      f"got {recipes._CONFIG_HASH_SCHEMA}")

label2 = ("[2] pydantic preserves the None / '' distinction"
          if PYDANTIC_IS_REAL
          else "[2] None / '' distinction  (STUBBED pydantic -- proves nothing about real pydantic)")
print("\n" + label2)
r_absent = make()
r_empty = make(entrypoint="")
r_value = make(entrypoint="/usr/bin/env")
check("absent -> None", r_absent.entrypoint is None, repr(r_absent.entrypoint))
check("'' stays '' (not coerced to None)", r_empty.entrypoint == ""
      and r_empty.entrypoint is not None, repr(r_empty.entrypoint))
check("value round-trips", r_value.entrypoint == "/usr/bin/env",
      repr(r_value.entrypoint))

print("\n[3] all three hash differently")
h_absent = recipes.compute_config_hash(r_absent, "2_node")
h_empty = recipes.compute_config_hash(r_empty, "2_node")
h_value = recipes.compute_config_hash(r_value, "2_node")
check("None != ''", h_absent != h_empty, f"{h_absent} vs {h_empty}")
check("None != value", h_absent != h_value, f"{h_absent} vs {h_value}")
check("'' != value", h_empty != h_value, f"{h_empty} vs {h_value}")

print("\n[4] hashing is stable across calls")
check("same recipe hashes identically twice",
      recipes.compute_config_hash(r_empty, "2_node") == h_empty)

print("\n[5] payload carries entrypoint explicitly")
p_absent = recipes.build_config_payload(r_absent, "2_node")
p_empty = recipes.build_config_payload(r_empty, "2_node")
check("key present even when None", "entrypoint" in p_absent)
check("None recorded as null", p_absent["entrypoint"] is None,
      repr(p_absent["entrypoint"]))
check("'' recorded as ''", p_empty["entrypoint"] == "",
      repr(p_empty["entrypoint"]))
check("payload _schema == 5", p_absent["_schema"] == 5)

print("\n[6] payload otherwise unchanged for entrypoint-less recipes")
expected_keys = {
    "_schema", "hf_path", "image", "entrypoint", "model_path_override",
    "extra_mounts", "launch_argv_prefix", "gpu_util", "max_model_len",
    "tp_size", "pp_size", "env_vars", "vllm_args", "mods",
}
# NOTE gpu_util_ceiling_exempt is deliberately ABSENT -- see section [10].
check("payload keys are exactly the expected set",
      set(p_absent) == expected_keys,
      f"unexpected: {set(p_absent) ^ expected_keys}")

print("\n[7] SABOTAGE: prove the harness catches a collapse of '' -> None")
_orig = recipes.build_config_payload


def _sabotaged(recipe, topo_key):
    payload = _orig(recipe, topo_key)
    payload["entrypoint"] = payload["entrypoint"] or None  # truthiness bug
    return payload


recipes.build_config_payload = _sabotaged
sab_absent = recipes.compute_config_hash(r_absent, "2_node")
sab_empty = recipes.compute_config_hash(r_empty, "2_node")
recipes.build_config_payload = _orig
check("truthiness bug WOULD collide None with '' (harness is live)",
      sab_absent == sab_empty,
      "sabotage did not reproduce -- harness may not be testing what it claims")

print("\n[8] schema 4 additions: model_path_override / extra_mounts")
r_plain = make()
r_override = make(model_path_override="/models/glm-5.3-flash-nvfp4")
r_mounts = make(extra_mounts=["/var/tmp/x:/models/x"])
h_plain = recipes.compute_config_hash(r_plain, "2_node")
h_override = recipes.compute_config_hash(r_override, "2_node")
h_mounts = recipes.compute_config_hash(r_mounts, "2_node")
check("model_path_override changes hash", h_plain != h_override,
      f"{h_plain} vs {h_override}")
check("extra_mounts changes hash", h_plain != h_mounts,
      f"{h_plain} vs {h_mounts}")

p_plain = recipes.build_config_payload(r_plain, "2_node")
check("payload has model_path_override key", "model_path_override" in p_plain)
check("payload has extra_mounts key", "extra_mounts" in p_plain)
check("default model_path_override is None", p_plain["model_path_override"] is None)
check("default extra_mounts is []", p_plain["extra_mounts"] == [])

print("\n[9] extra_mounts hashes order-independent (sorted before hashing)")
r_m1 = make(extra_mounts=["/a:/a", "/b:/b"])
r_m2 = make(extra_mounts=["/b:/b", "/a:/a"])
h_m1 = recipes.compute_config_hash(r_m1, "2_node")
h_m2 = recipes.compute_config_hash(r_m2, "2_node")
check("reordered extra_mounts hash identically", h_m1 == h_m2, f"{h_m1} vs {h_m2}")

print("\n[10] schema 5: launch_argv_prefix is hashed, ORDER-SIGNIFICANT")
r_argv_none = make()
r_argv_serve = make(launch_argv_prefix=["vllm", "serve", "{model}"])
r_argv_rev = make(launch_argv_prefix=["serve", "vllm", "{model}"])
h_argv_none = recipes.compute_config_hash(r_argv_none, "2_node")
h_argv_serve = recipes.compute_config_hash(r_argv_serve, "2_node")
h_argv_rev = recipes.compute_config_hash(r_argv_rev, "2_node")
check("None (module path) != ['vllm','serve']", h_argv_none != h_argv_serve,
      f"{h_argv_none} vs {h_argv_serve}")
check("argv order is significant (it IS an argv)", h_argv_serve != h_argv_rev,
      f"{h_argv_serve} vs {h_argv_rev}")
check("payload records it as a list", recipes.build_config_payload(
    r_argv_serve, "2_node")["launch_argv_prefix"] == ["vllm", "serve", "{model}"])
check("payload records None as null", recipes.build_config_payload(
    r_argv_none, "2_node")["launch_argv_prefix"] is None)

print("\n[11] gpu_util_ceiling_exempt is EXCLUDED from the hash, deliberately")
# The exclusion is dated 2026-09-08 in compute_config_hash()'s docstring:
# the field gates WHETHER a deploy proceeds, never what the container runs.
# Two recipes differing only in it launch byte-identical containers, so
# hashing it would orphan every recorded launch for no behavioural change.
# Asserted here so the exclusion cannot be reversed silently -- the `mods`
# exclusion rested on the same premise and went stale without anyone noticing.
r_exempt = make(gpu_util_ceiling_exempt=True)
check("exempt does NOT change the hash",
      recipes.compute_config_hash(r_exempt, "2_node") == h_plain,
      f"{recipes.compute_config_hash(r_exempt, '2_node')} vs {h_plain}")
check("exempt absent from the payload entirely",
      "gpu_util_ceiling_exempt" not in recipes.build_config_payload(r_exempt, "2_node"))

print("\n" + "=" * 60)
if FAILURES:
    print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
    sys.exit(1)
print("ALL CHECKS PASSED")
