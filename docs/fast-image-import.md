# Proposed fast-image import workflow

This is a design for a later phase of the `exact-boot-image` branch. The current
branch can select Anton's v4b image or an image ID supplied by the user. It does
not download, upload, or import an image.

## User experience

Setup may ask:

> Offer the experimental fast H100 image when compatible? This can later import
> a persistent custom image into the project you choose. No image is imported
> during setup. Default: No.

Choosing Yes records a local preference only. The actual cloud action waits
until the user chooses a single H100, `eu-north1`, and a destination project.
Before that first launch, the plugin shows a separate import review containing:

- release version, source URL, SHA-256 and expected download size;
- destination project, region, bucket/object and Compute image name;
- persistent resources and estimated storage charges;
- expected import duration and the option to use the public Ubuntu image;
- the tested scope and the distinction between guest boot and total launch time.

Confirmation starts a resumable background job. Cancellation before confirmation
changes nothing. Leaving the UI after submission does not cancel the import.
The plugin records every created resource and reconciles by immutable ID before
retrying, so it never creates a second image after an uncertain response.

## Supported data path

Nebius documents this import sequence: put the image file in Object Storage,
then create a Compute image from that bucket and object. The bucket, image and
VM should be in the same region. The portable workflow should therefore be:

1. Fetch a small, versioned release manifest over HTTPS.
2. Require the manifest's SHA-256 to match a digest pinned in the plugin release.
3. Create or reuse a plugin-owned bucket in the selected project and region.
4. Copy the public QCOW2 object into that bucket, verifying its pinned SHA-256.
5. Create a project-local Compute image from the object.
6. Wait for `READY`, verify minimum disk size and record the returned image ID.
7. Launch through the existing exact-image preflight and final review.
8. Offer to delete the staging object after the Compute image is ready. Keep the
   Compute image until the user explicitly removes it.

Direct import from a bucket in another tenant may work in some configurations,
but it is not the documented path and must not be the default without an
end-to-end cross-tenant test. Copying into the recipient's bucket also gives a
clear ownership and recovery boundary.

## Release contract

The publisher must provide one immutable manifest per image version and region.
It needs at least:

- schema and release version;
- HTTPS artifact URL, object size, format and SHA-256;
- CPU architecture, boot mode and minimum/recommended disk size;
- supported region, platform and GPU count;
- default SSH user and cloud-init compatibility;
- source image/build commit, package manifest and validation report;
- guest-boot measurement definition and sample size;
- deprecation or revocation status.

The release must be generalized again after export and re-imported into an
isolated project for validation. Host keys, machine identity, cloud-init state,
logs, shell history, tokens, SSH private keys and provider metadata must not be
present. The digest published with the same mutable object is insufficient;
the plugin release must pin it or verify a separately signed manifest.

## Permissions and cleanup

Nebius currently documents `admin` access in the destination tenant or project
as a prerequisite for importing a custom image. Setup should detect missing
permission and keep the public-family path available. The importer must disclose
and track the bucket, staging object, transfer credentials or service account,
transfer job, and Compute image it creates.

Temporary credentials must stay out of command history, process arguments, logs
and persistent state. Delete them after transfer. A failed import keeps enough
IDs for repair or explicit cleanup and never deletes unrelated resources. Plugin
uninstall should report imported images and their ongoing cost, then leave them
unless the user separately confirms cloud deletion.

## Delivery order

1. Export v4b and audit the portable file.
2. Re-import and validate it in an isolated project.
3. Publish the immutable artifact and release manifest in `eu-north1`.
4. Prototype and validate the recipient-owned bucket copy.
5. Add the setup preference and first-use import job.
6. Test interruption, duplicate prevention, permissions, cleanup and upgrades.
7. Enable the prompt only after the hosted artifact passes the round trip.
