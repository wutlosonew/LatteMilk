"""Conservative, one-instance Oracle Always Free A1 launcher.

Read-only checks fail closed. This script never changes existing resources.
"""

import os
import re
import sys
import tempfile
from pathlib import Path

import oci


REGION = "ap-singapore-1"
SHAPE = "VM.Standard.A1.Flex"
NAME = "lt-screen"
OCPUS = 2
MEMORY_GB = 12
BOOT_GB = 50
STORAGE_LIMIT_GB = 200
# OCI Balanced performance tier for boot volumes (10 VPUs per GB).
BALANCED_VPUS = 10
EXCLUDED_STATES = {"TERMINATED"}


def all_pages(call, **kwargs):
    return oci.pagination.list_call_get_all_results(call, **kwargs).data


def visible_compartments(identity, tenancy_id):
    # Enumerate the tenancy root plus every ACTIVE child recursively. A missing
    # list permission is an error: we cannot establish the storage/A1 total.
    children = all_pages(
        identity.list_compartments, tenancy_id,
        compartment_id_in_subtree=True, access_level="ACCESSIBLE",
    )
    return [tenancy_id] + [c.id for c in children if c.lifecycle_state == "ACTIVE"]


def check_existing_and_storage(compute, block, compartments, ads):
    used = 0
    for compartment in compartments:
        for instance in all_pages(compute.list_instances, compartment):
            if instance.lifecycle_state in EXCLUDED_STATES:
                continue
            if instance.shape == SHAPE or instance.display_name == NAME:
                print("Existing active A1 or lt-screen found; no instance created.")
                return False
        for volume in all_pages(block.list_volumes, compartment):
            if volume.lifecycle_state not in EXCLUDED_STATES:
                used += volume.size_in_gbs
        for ad in ads:
            for volume in all_pages(
                block.list_boot_volumes, ad.name, compartment_id=compartment
            ):
                if volume.lifecycle_state not in EXCLUDED_STATES:
                    used += volume.size_in_gbs
    print(f"Visible active boot + block volume storage: {used} GB")
    if used + BOOT_GB > STORAGE_LIMIT_GB:
        print("A new 50 GB boot volume could exceed 200 GB; no instance created.")
        return False
    return True


def choose_image(compute, compartment, ad):
    images = all_pages(
        compute.list_images, compartment, operating_system="Canonical Ubuntu",
        shape=SHAPE,
    )
    candidates = []
    for img in images:
        name = img.display_name or ""
        version = img.operating_system_version or ""
        if (img.lifecycle_state != "AVAILABLE" or not version.startswith("22.04")
                or not re.search(r"(?:aarch64|arm64)", name, re.I)):
            continue
        # Confirm the platform image is supported by A1, not just its name.
        entries = all_pages(
            compute.list_image_shape_compatibility_entries, img.id,
        )
        if not any(entry.shape == SHAPE for entry in entries):
            continue
        candidates.append(img)
    if not candidates:
        raise RuntimeError(f"No compatible Ubuntu 22.04 ARM64 platform image in {ad.name}")
    normal = [img for img in candidates if "minimal" not in img.display_name.lower()]
    return max(normal or candidates, key=lambda img: img.time_created).id


def out_of_capacity(exc):
    if exc.status not in (400, 409, 500, 503):
        return False
    message = f"{exc.code} {exc.message}".lower()
    return any(phrase in message for phrase in (
        "out of capacity", "out of host capacity", "outofcapacity",
        "outofhostcapacity", "out of host", "capacity unavailable",
    ))


def main(key_path):
    config = {
        "user": os.environ["OCI_USER_OCID"],
        "fingerprint": os.environ["OCI_FINGERPRINT"],
        "tenancy": os.environ["OCI_TENANCY_OCID"],
        "region": REGION,
        "key_file": str(key_path),
    }
    oci.config.validate_config(config)
    identity = oci.identity.IdentityClient(config)
    compute = oci.core.ComputeClient(config)
    block = oci.core.BlockstorageClient(config)
    network = oci.core.VirtualNetworkClient(config)
    tenancy = config["tenancy"]
    compartment = os.environ["OCI_COMPARTMENT_OCID"]
    subnet_id = os.environ["OCI_SUBNET_OCID"]
    ads = all_pages(identity.list_availability_domains, tenancy)
    if not ads:
        raise RuntimeError("No availability domains visible; stopping safely")
    compartments = visible_compartments(identity, tenancy)
    if compartment not in compartments:
        raise RuntimeError("Target compartment is not in visible active compartments")
    subnet = network.get_subnet(subnet_id).data
    if subnet.prohibit_public_ip_on_vnic:
        raise RuntimeError("Existing subnet prohibits public IPv4")
    if not check_existing_and_storage(compute, block, compartments, ads):
        return

    ssh_key = os.environ["OCI_SSH_PUBLIC_KEY"].strip()
    if not re.match(r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-\S+)\s+\S+", ssh_key):
        raise RuntimeError("OCI_SSH_PUBLIC_KEY must be a public SSH key")

    for ad in ads:
        if subnet.availability_domain and subnet.availability_domain != ad.name:
            continue
        shapes = all_pages(
            compute.list_shapes, compartment, availability_domain=ad.name
        )
        if not any(shape.shape == SHAPE for shape in shapes):
            print(f"{ad.name}: A1 shape not listed; skipping")
            continue
        image_id = choose_image(compute, compartment, ad)
        details = oci.core.models.LaunchInstanceDetails(
            availability_domain=ad.name,
            compartment_id=compartment,
            display_name=NAME,
            shape=SHAPE,
            shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
                ocpus=OCPUS, memory_in_gbs=MEMORY_GB,
            ),
            source_details=oci.core.models.InstanceSourceViaImageDetails(
                source_type="image", image_id=image_id,
                boot_volume_size_in_gbs=BOOT_GB,
                boot_volume_vpus_per_gb=BALANCED_VPUS,
            ),
            create_vnic_details=oci.core.models.CreateVnicDetails(
                subnet_id=subnet_id, assign_public_ip=True,
            ),
            metadata={"ssh_authorized_keys": ssh_key},
        )
        try:
            instance = compute.launch_instance(details).data
        except oci.exceptions.ServiceError as exc:
            if out_of_capacity(exc):
                print(f"{ad.name}: A1 out of capacity; trying next AD")
                continue
            raise
        print(f"Instance OCID: {instance.id}")
        print(f"Shape: {instance.shape}")
        print(f"OCPU: {instance.shape_config.ocpus}")
        print(f"RAM: {instance.shape_config.memory_in_gbs} GB")
        print(f"Region: {REGION}")
        print(f"Lifecycle state: {instance.lifecycle_state}")
        print("Instance created; future runs will detect it and skip creation.")
        return
    print("No A1 capacity in eligible availability domains; next scheduled run will retry.")


if __name__ == "__main__":
    required = ("OCI_TENANCY_OCID", "OCI_USER_OCID", "OCI_FINGERPRINT",
                "OCI_PRIVATE_KEY", "OCI_COMPARTMENT_OCID", "OCI_SUBNET_OCID",
                "OCI_SSH_PUBLIC_KEY")
    missing = [name for name in required if not os.environ.get(name, "").strip()]
    if missing:
        sys.exit("Missing secrets: " + ", ".join(missing))
    with tempfile.TemporaryDirectory(prefix="oci-a1-") as directory:
        key_path = Path(directory) / "oci_api_key.pem"
        key_path.write_text(os.environ["OCI_PRIVATE_KEY"], encoding="utf-8")
        key_path.chmod(0o600)
        main(key_path)
