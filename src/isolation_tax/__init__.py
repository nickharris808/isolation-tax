"""isolation-tax — what does per-tenant KV-cache isolation cost you?"""
__version__ = "0.1.0"

from .core import (BOUNDED, EXACT, Request, TaxResult, measure,  # noqa: E402
                   public_share_arm)
from .fleet import (MEASURED_ENVELOPE, MEASURED_LADDER, FleetInputs,  # noqa: E402
                    OutOfEnvelope, check_envelope, fleet_delta, kv_bytes_per_token,
                    max_kv_weight_ratio, retention_band, throughput_ratio_band)
# Imported last: signoff reads __version__ from this module, so it must be bound first.
from .signoff import build_certificate  # noqa: E402

# NOTE: `throughput_ratio` -- the (1+r)/(1+r*keep) closed form -- was REMOVED, not deprecated.
# The A100 model-size ladder falsified it in direction (see fleet.py). Leaving it importable
# would leave a falsified conversion one `from isolation_tax import ...` away from a dollar figure.
__all__ = ["measure", "public_share_arm", "build_certificate", "Request", "TaxResult",
           "EXACT", "BOUNDED", "FleetInputs", "fleet_delta", "kv_bytes_per_token",
           "max_kv_weight_ratio", "retention_band", "throughput_ratio_band", "check_envelope",
           "OutOfEnvelope", "MEASURED_LADDER", "MEASURED_ENVELOPE", "__version__"]
