# Copyright 2022 Shawn Qureshi and individual contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import boto3
from botocore.config import Config
import dask
import os
import sys
import shutil
import re
import time
import datetime
from . import codeartifact
from . import artifactory
from . import caching
from . import monitor
from . import boto_setup


# Helper to fetch version metadata from Artifactory
def get_artifactory_version_metadata(package_dict, version):
    """
    Fetch metadata for a specific version from Artifactory.
    Returns a dict with at least 'created' or 'lastModified' keys if available.
    """
    # Build the path to the version directory
    repo = package_dict["repository"]
    pkg = package_dict["package"]
    pkg_type = package_dict.get("type")
    if pkg_type == "npm":
        pkg_base = pkg.split("/")[-1] if "/" in pkg else pkg
        tgz_filename = f"{pkg_base}-{version}.tgz"
        tgz_path = f"/api/storage/{repo}/{pkg}/-/{tgz_filename}"
        args = package_dict.get("args")
        if args is None:
            args = sys.modules[__name__].__dict__.get("args", None)
        try:
            logger.debug(
                f"Fetching Artifactory metadata for {repo}/{pkg}/{version} via {tgz_path}"
            )
            meta = artifactory.artifactory_npm_metadata_fetch(args, package_dict)
            if isinstance(meta, dict) and (
                meta.get("created") or meta.get("lastModified")
            ):
                return meta
        except Exception as e:
            logger.warning(
                f"Failed to fetch npm metadata for {repo}/{pkg}@{version} via {tgz_path}: {e}"
            )
        return {}
    else:
        # Compose the API call to the folder for this version
        # e.g. /api/storage/<repo>/<pkg>/<version>
        path = f"/api/storage/{repo}/{pkg}/{version}"
    try:
        args = package_dict.get("args")
        if args is None:
            # Fallback: try global args if available
            import sys

            args = sys.modules[__name__].__dict__.get("args", None)
        logger.debug(
            f"Fetching Artifactory metadata for {repo}/{pkg}/{version} via {path}"
        )
        # Use raise_on_error=True so 404s (version path doesn't exist as a folder)
        # are caught by the except block below instead of calling sys.exit(1).
        meta = artifactory.artifactory_http_call(args, path, raise_on_error=True)
        return meta if isinstance(meta, dict) else {}
    except Exception as e:
        logger.warning(f"Failed to fetch metadata for {repo}/{pkg}/{version}: {e}")
        return {}


# --- Repo mapping support ---
def get_repo_mapping():
    """
    Reads ACM_REPO_MAPPING from environment and returns a dict mapping Artifactory repo to CodeArtifact repo.
    Format: 'artifactory1:codeartifact1,artifactory2:codeartifact2'
    """
    mapping_str = os.environ.get("ACM_REPO_MAPPING")
    mapping = {}
    if mapping_str:
        for pair in mapping_str.split(","):
            if ":" in pair:
                artifactory_repo, codeartifact_repo = pair.split(":", 1)
                mapping[artifactory_repo.strip()] = codeartifact_repo.strip()
    return mapping


def map_repo_name(artifactory_repo):
    mapping = get_repo_mapping()
    return mapping.get(artifactory_repo, artifactory_repo)


logger = monitor.getLogger()

supported_packages = ["maven", "pypi", "npm", "gradle"]

# The replication path all replication processing will be saved to temporarily during replication
replication_path = ".replication"

# Codeartifact token refresh
token_refresh = 5  # hours

db_file = ""


def get_packagename(package):
    """
    get_packagename strips namespace.

    :param package: package variable to strip
    :return: name of the package
    """
    return package.split("/")[-1]


def get_package_type(repository, artifactory_repos):
    """
    get_package_type fetches the repository package manager type

    :param repository: repository name to inspect
    :param artifactory_repos: dictionary of Artifactory /api/storageinfo repositoriesSummaryList
    """
    success = False
    for repo in artifactory_repos:
        if repo["repoKey"] == repository:
            success = True
            repo_to_check = repo
            break
    if not success:
        logger.critical(
            f"Repository {repository} not found in Artifactory list retrieved"
        )
        sys.exit(1)
    if repo_to_check.get("packageType"):
        if repo_to_check["repoType"] == "LOCAL":
            return repo_to_check["packageType"].lower()
    else:
        logger.critical(f"Repo missing packageType key:\n{str(repo_to_check)}")
        sys.exit(1)


def check_artifactory_repos(repos, artifactory_repos):
    """
    check_artifactory_repos verifies a repository is in the api listing from Artifactory

    :param repos: space seperated list of repositories to check
    :param artifactory_repos: dictionary of Artifactory /api/storageinfo repositoriesSummaryList
    """
    success_all = []
    for repo in repos.split(" "):
        # If mapping is used, map CodeArtifact repo name back to Artifactory repo name for checking
        artifactory_repo = repo
        mapping = get_repo_mapping()
        # If this repo is a mapped CodeArtifact repo, find the Artifactory source name
        for k, v in mapping.items():
            if v == repo:
                artifactory_repo = k
                break
        success = {"repository": artifactory_repo, "success": False}
        for repository in artifactory_repos:
            if repository["repoKey"] == artifactory_repo:
                success["success"] = True
        success_all.append(success)
    for result in success_all:
        if result["success"] == True:
            logger.info(
                f"Repository check for {result['repository']} in Artifactory listing succeeded"
            )
        else:
            logger.critical(
                f"Repository {result['repository']} not found in Artifactory listing"
            )
            sys.exit(1)


def append_package_specific_keys(args, package_dict):
    """
    append_package_specific_keys adds any special keys based on package manager type.

    :param args: arguments passed to cli command
    :param package_dict: standard package dictionary to inspect
    :return: package dictionary plus any special keys
    """
    if package_dict["type"] == "npm":
        package_dict["metadata"] = artifactory.artifactory_npm_metadata_fetch(
            args, package_dict
        )

    if package_dict["type"] == "maven" or package_dict["type"] == "gradle":
        package_name_split = package_dict["package"].split("/")
        package_name_split.remove(package_dict["package"].split("/")[-1])
        package_dict["namespace"] = "/".join(package_name_split)

    return package_dict


def get_artifactory_package_versions(binaries, package_dict):
    """
    get_artifactory_package_versions gets all versions of a given package from
    Artifactory. Each package manager type is inspected and versions are returned
    based on the style of the package manager.

    :param binaries: list of binary uris from Artifactory /api/search/artifact
    :param package_dict: standard package dictionary to inspect
    :return: list of uris of the specific version
    """
    versions = set()
    pkg = package_dict.get("package")
    pkg_type = package_dict.get("type")
    for uri in binaries:
        version = None
        if pkg_type == "npm":
            try:
                version = (
                    uri.split("/" + pkg + "/")[-1]
                    .split(pkg + "-")[1]
                    .split(".tgz")[0]
                    .split(".json")[0]
                )
            except Exception as e:
                logger.debug(
                    f"[get_artifactory_package_versions] Failed to extract npm version from {uri}: {e}"
                )
        elif pkg_type in ["pypi", "maven", "gradle"]:
            parts = uri.split("/" + pkg + "/")
            if len(parts) > 1:
                after_pkg = parts[-1]
                after_pkg_parts = after_pkg.strip("/").split("/")
                # The version is the parent directory of the file
                if len(after_pkg_parts) >= 2:
                    version_candidate = after_pkg_parts[-2]
                elif len(after_pkg_parts) == 1:
                    # If it's just a directory (no file), treat as version
                    version_candidate = after_pkg_parts[0]
                else:
                    version_candidate = None
                # Only add if it looks like a version (not a file extension)
                if version_candidate and not any(
                    version_candidate.endswith(ext)
                    for ext in [".pom", ".jar", ".tar.gz", ".whl", ".egg"]
                ):
                    version = version_candidate
                else:
                    version = None
        if version is not None and isinstance(version, str) and version.strip():
            versions.add(version.strip())
        else:
            logger.debug(
                f"[get_artifactory_package_versions] Skipping invalid or empty version extracted from {uri}: '{version}'"
            )
    return sorted(versions)


def get_filtered_artifactory_package_versions(
    binaries, package_dict, artifactory_api_func, min_versions=5, max_age_days=365
):
    """
    Extracts versions from binaries and filters them by date/count in a single pass.
    Returns only versions from the last year or the last N versions (whichever is more).
    - binaries: list of binary uris from Artifactory /api/search/artifact
    - package_dict: standard package dictionary to inspect
    - artifactory_api_func: function to get version metadata (must return dict with 'created' or 'lastModified')
    - min_versions: minimum number of versions to keep
    - max_age_days: how many days back to keep (default: 365)
    Returns: filtered list of versions
    """
    version_dates = []
    pkg = package_dict.get("package")
    pkg_type = package_dict.get("type")
    unique_versions = set()
    uri_map = {}  # version -> example uri (for debug)
    if pkg_type == "npm":
        for uri in binaries:
            try:
                import urllib.parse

                # Get the filename (after last slash)
                filename = uri.split("/")[-1]
                filename = urllib.parse.unquote(filename)
                # Remove .tgz or .json extension
                if filename.endswith(".tgz"):
                    filename = filename[:-4]
                elif filename.endswith(".json"):
                    filename = filename[:-5]
                # For npm, version is after the last hyphen in the filename
                # e.g. '@bird/axios-1.0.33+df0bb98' -> version is '1.0.33+df0bb98'
                pkg_base = pkg.split("/")[-1] if "/" in pkg else pkg
                # Find the last occurrence of pkg_base + '-' in the filename
                prefix = pkg_base + "-"
                idx = filename.rfind(prefix)
                version = None
                if idx != -1:
                    version = filename[idx + len(prefix) :]
                else:
                    # fallback: take everything after the first hyphen
                    parts = filename.split("-", 1)
                    version = parts[1] if len(parts) > 1 else None
                if version and version.strip():
                    unique_versions.add(version.strip())
                    uri_map[version.strip()] = uri
            except Exception as e:
                logger.debug(
                    f"[get_filtered_artifactory_package_versions] Failed to extract npm version from {uri}: {e}"
                )
    elif pkg_type in ["pypi", "maven", "gradle"]:
        for uri in binaries:
            parts = uri.split("/" + pkg + "/")
            if len(parts) > 1:
                after_pkg = parts[-1]
                after_pkg_parts = after_pkg.strip("/").split("/")
                if len(after_pkg_parts) >= 2:
                    version_candidate = after_pkg_parts[-2]
                elif len(after_pkg_parts) == 1:
                    version_candidate = after_pkg_parts[0]
                else:
                    version_candidate = None
                # Only add if it looks like a version (not a file extension)
                if version_candidate and not any(
                    version_candidate.endswith(ext)
                    for ext in [
                        ".pom",
                        ".jar",
                        ".tar.gz",
                        ".whl",
                        ".egg",
                        ".module",
                        ".sha512",
                    ]
                ):
                    unique_versions.add(version_candidate.strip())
                    uri_map[version_candidate.strip()] = uri
    # Now process each unique version only once
    for version in unique_versions:
        # Fetch metadata and parse date here
        meta = artifactory_api_func(package_dict, version)
        logger.debug(
            f"[get_filtered_artifactory_package_versions] Metadata for version '{version}': {meta}"
        )
        date_str = meta.get("created") or meta.get("lastModified")
        dt = None
        if date_str:
            dt = None
            # Try Python 3.7+ fromisoformat after stripping 'Z'
            iso_str = date_str.rstrip("Z")
            try:
                dt = datetime.datetime.fromisoformat(iso_str)
            except Exception:
                # Fallback to strptime for common patterns
                tried_formats = [
                    "%Y-%m-%dT%H:%M:%S",
                    "%Y-%m-%dT%H:%M:%S.%f",
                    "%Y-%m-%dT%H:%M:%SZ",
                    "%Y-%m-%dT%H:%M:%S.%fZ",
                ]
                for fmt in tried_formats:
                    try:
                        dt = datetime.datetime.strptime(date_str, fmt)
                        break
                    except Exception:
                        continue
            if dt is None:
                logger.debug(
                    f"[get_filtered_artifactory_package_versions] Failed to parse date '{date_str}' for version '{version}' with ISO8601 or known formats."
                )
        else:
            logger.debug(
                f"[get_filtered_artifactory_package_versions] No date found for version '{version}'"
            )
        version_dates.append((version, dt))
    # If no versions found, log for debug
    if not unique_versions:
        logger.debug(
            f"[get_filtered_artifactory_package_versions] No valid versions found in binaries for package {pkg}"
        )

    # Sort by date descending (newest first)
    dated_versions = [x for x in version_dates if x[1] is not None]
    undated_versions = [x for x in version_dates if x[1] is None]
    dated_versions.sort(key=lambda x: x[1], reverse=True)
    cutoff = datetime.datetime.now() - datetime.timedelta(days=max_age_days)
    last_year_versions = [v for v, dt in dated_versions if dt >= cutoff]
    last_n_versions = [v for v, dt in dated_versions[:min_versions]]
    if not dated_versions and undated_versions:
        last_n_versions = [v for v, dt in undated_versions[:min_versions]]
    if len(last_year_versions) >= len(last_n_versions):
        filtered = last_year_versions.copy()
        logger.info(
            f"[get_filtered_artifactory_package_versions] {pkg} - Using all versions from last {max_age_days} days: {filtered}"
        )
    else:
        filtered = last_n_versions.copy()
        logger.info(
            f"[get_filtered_artifactory_package_versions] {pkg} - Using last {min_versions} versions by date: {filtered}"
        )
    undated = [v for v, dt in undated_versions]
    if undated:
        logger.info(
            f"[get_filtered_artifactory_package_versions] {pkg} - Including undated versions (fallback): {undated}"
        )
        filtered += [v for v in undated if v not in filtered]
    filtered = [v for v, _ in version_dates if v in filtered]
    all_versions = [v for v, _ in version_dates]
    filtered_out = [v for v, _ in version_dates if v not in filtered]

    logger.info(
        f"[get_filtered_artifactory_package_versions] {pkg} - All available versions: {all_versions}"
    )
    logger.info(
        f"[get_filtered_artifactory_package_versions] {pkg} - Selected for migration: {filtered}"
    )
    if filtered_out:
        logger.info(
            f"[get_filtered_artifactory_package_versions] {pkg} - Filtered out (not migrated): {filtered_out}"
        )

    # Always-visible summary (shows without --verbose or --debug)
    logger.info(
        f"[version-filter] {pkg} | total={len(all_versions)} "
        f"selected={len(filtered)} filtered_out={len(filtered_out)} "
        f"| kept={filtered} | skipped={filtered_out}"
    )
    return filtered


def replicate_package(args, client, token_codeartifact, package_dict, db_file):
    """
    replicate_package fetches uris of the binaries associated with a package. It
    first checks to see if a version was specified and passes on to the next part.
    Otherwise it will first determine all versions of a supplied package and make
    a list to process later. Then it checks to see if that package version already
    exists in codeartifact. If the check reports that the artifact version was
    pushed to codeartifact, but it's status isn't Published, it will wipe that
    artifact from codeartifact and continue with replication. If it is fully
    published already in codeartifact it will skip replication. Finally, it will
    fetch the binaries associated with the package version, and upload them to
    codeartifact.

    :param args: command line arguments
    :param client: api client to use with codeartifact
    :param token_codeartifact: token generated from codeartifact
    :param package_dict: standard package dictionary to inspect
    :return success boolean:
    """
    success = True
    all_packages_published = False
    packages_to_replicate_temp = []
    if args.cache:
        # Insert package root if it's not in cache
        if not caching.check_package(
            args, package_dict["package"], package_dict["repository"], db_file
        ):
            caching.insert_package(
                args, package_dict["package"], package_dict["repository"], db_file
            )
    if not package_dict.get("version"):
        all_packages_published = True
        # Get all versions of the package and append to list to replicate
        skip = False
        if args.cache:
            if caching.check_all_versions_published(
                args, package_dict["package"], package_dict["repository"], db_file
            ):
                logger.info(
                    f"Cache: All versions of package {package_dict['package']} in repository {package_dict['repository']} already published, skipping'"
                )
                return {
                    "package": package_dict["package"],
                    "version": package_dict["version"],
                    "published": True,
                }
            if caching.check_all_versions_fetched(
                args, package_dict["package"], package_dict["repository"], db_file
            ):
                # Check if all versions were fetched, append if so and skip binary search
                logger.info(
                    f"Cache: All versions of package {package_dict['package']} in repository {package_dict['repository']} already fetched, using cached list."
                )
                for version in caching.fetch_all_versions(
                    args, package_dict["package"], package_dict["repository"], db_file
                ):
                    package_full = package_dict
                    package_full["version"] = version
                    if package_full not in packages_to_replicate_temp:
                        packages_to_replicate_temp.append(package_full)
                skip = True
        if skip == False:
            # Parse each uri and generate packages to check and replicate
            for uri in artifactory.artifactory_package_binary_search(
                args, package_dict
            ):
                if skip == False:
                    version = ""
                    if package_dict.get("type") == "npm":
                        ## ToDo: It might be better to search .npm metadata to fetch versions
                        if re.search(".tgz$", uri):
                            version = (
                                uri.split("/")[-1]
                                .split(package_dict.get("package"))[-1]
                                .replace(".tgz", "")
                            )
                    elif package_dict.get("type") in ["pypi", "maven", "gradle"]:
                        version = uri.split("/" + package_dict.get("package") + "/")[
                            1
                        ].split("/")[0]
                    else:
                        print(
                            "WARNING: Package type "
                            + package_dict.get("type")
                            + " not supported yet."
                        )
                    if version == "":
                        print(
                            "WARNING: Unable to replicate "
                            + package_dict.get("package")
                            + ": No versions found in binary search"
                        )
                    else:
                        package_full = package_dict
                        package_full["version"] = version
                        if package_full not in packages_to_replicate_temp:
                            packages_to_replicate_temp.append(package_full)
                            if args.cache:
                                # Insert package version if not in cache yet
                                if not caching.check_package_version(
                                    args,
                                    package_dict["package"],
                                    package_dict["repository"],
                                    package_dict["version"],
                                    db_file,
                                ):
                                    caching.insert_package_version(
                                        args,
                                        package_dict["package"],
                                        package_dict["repository"],
                                        package_dict["version"],
                                        db_file,
                                    )
        if args.cache:
            caching.set_all_versions_fetched(
                args, package_dict["package"], package_dict["repository"], db_file
            )
    else:
        if args.cache:
            # Insert package version if not in cache yet
            if not caching.check_package_version(
                args,
                package_dict["package"],
                package_dict["repository"],
                package_dict["version"],
                db_file,
            ):
                caching.insert_package_version(
                    args,
                    package_dict["package"],
                    package_dict["repository"],
                    package_dict["version"],
                    db_file,
                )
        packages_to_replicate_temp.append(package_dict)

    """
  Here we check to see if each package version already exists in codeartifact.
  If a package version exists but it's not in Published status, we delete it and
  add to the replication. If a package version does exist and it's Published, we
  skip that package version.
  """
    packages_to_replicate = []
    for temp_dict in packages_to_replicate_temp:
        skip = False
        if args.cache:
            if caching.check_version_published(
                args,
                temp_dict["package"],
                temp_dict["repository"],
                temp_dict["version"],
                db_file,
            ):
                logger.info(
                    f"Cache: Package {temp_dict['package']} version {temp_dict['version']} in repository {temp_dict['repository']} already published, skipping."
                )
                skip = True

        if skip == False:
            check_result = codeartifact.codeartifact_check_package_version(
                args, client, temp_dict
            )
            if check_result == 2:
                # This means the package was not fully published and will be wiped and published
                codeartifact.codeartifact_wipe_package_version(args, client, temp_dict)
            if check_result in [1, 2]:
                packages_to_replicate.append(temp_dict)
            else:
                logger.info(
                    f"Package version found in codeartifact with status Published, skipping: {temp_dict['repository']} {temp_dict['package']} {temp_dict['version']}"
                )
                if args.cache:
                    if not caching.check_version_published(
                        args,
                        temp_dict["package"],
                        temp_dict["repository"],
                        temp_dict["version"],
                        db_file,
                    ):
                        logger.debug(
                            f"Cache: Codeartifact already shows artifact published, setting cache to published for {temp_dict['repository']} {temp_dict['package']} {temp_dict['version']}"
                        )
                        caching.set_package_version_to_published(
                            args,
                            temp_dict["package"],
                            temp_dict["repository"],
                            temp_dict["version"],
                            db_file,
                        )

    logger.debug(f"Packages to replicate: {packages_to_replicate}")
    regex = re.compile(r"[$&,:;=?#|'<>^*()%!\"\s\[\]]")
    for package in packages_to_replicate:
        publish_error = ""
        publish_fail = False
        
        # For npm packages, strip everything after '+' to make compatible with CodeArtifact
        if package["type"] == "npm" and "+" in package["version"]:
            original_version = package["version"]
            package["version"] = package["version"].split("+")[0]
            logger.info(
                f"npm version sanitized for CodeArtifact: {original_version} -> {package['version']}"
            )
        
        if re.search(regex, package["package"]) or re.search(regex, package["version"]):
            logger.warning(
                f"Bad characters found in package name or version, skipping: {package['repository']} {package['package']} {package['version']}"
            )
            success = False
        else:
            logger.info(
                f"Replicating {package['repository']} {package['package']} {package['version']}"
            )
            uris = artifactory.artifactory_package_binary_search(args, package)

            if package["type"] in supported_packages:
                foldername = (
                    package["package"].split("/")[-1] + f"-{package['version']}"
                )
            else:
                logger.critical(
                    f"Package type {package['type']} not supported: {package}"
                )
                sys.exit(1)
            uri_formatted = ""
            tree = "./" + replication_path + "/" + foldername
            """ToDo: We are encountering a problem where maven snapshot version binaries
      are creating their own version in codeartifact and making a mess in the UI.
      The final snapshot subversion does get set to published at the end though.
      - Pending AWS support
      """
            # For Maven/Gradle, sort URIs so that hash sidecar files (.sha512, .md5, .sha1)
            # are uploaded AFTER their corresponding artifacts. CodeArtifact rejects a hash
            # upload if the parent artifact hasn't been uploaded yet.
            if package["type"] in ["maven", "gradle"]:
                hash_exts = (".sha512", ".md5", ".sha1")
                uris = sorted(uris, key=lambda u: (1 if u.split("/")[-1].endswith(hash_exts) else 0))
            for uri in uris:
                uri_formatted = uri.replace("api/storage/", "")
                filename = uri.split("/")[-1]
                if args.dryrun:
                    logger.info(
                        f"Dryrun: Would download binary from Artifactory: {uri_formatted}"
                    )
                    logger.info(
                        f"Dryrun: Would upload binary to codeartifact: https://{package['endpoint']}/{package['package']}/{package['version']}"
                    )
                else:
                    artifactory.artifactory_binary_fetch(
                        args, uri_formatted, replication_path, foldername
                    )
                    response = codeartifact.codeartifact_upload_binary(
                        args,
                        client,
                        token_codeartifact,
                        package,
                        tree + "/" + filename,
                    )
                    logger.debug(f"Response: {response}")
                    if not response.ok:
                        publish_fail = True
                        if publish_error != "":
                            publish_error = publish_error + " -- "
                        error_detail = f"{response.status_code}, {response.reason}, {response.text}"
                        # Provide more helpful error message for metadata issues
                        if "Metadata is missing required fields" in response.text or response.reason == "missing_from_metadata":
                            error_detail += " (Package has invalid or missing metadata - Name/Version fields required by CodeArtifact)"
                        publish_error = publish_error + error_detail
            # Required after pushing maven jar/pom's
            if package["type"] in ["maven", "gradle"] and publish_fail == False:
                if args.dryrun:
                    logger.info(
                        f"Dryrun: Would upload maven-metadata.xml and set status to Published for {package['package']} {package['version']}"
                    )
                else:
                    # Try to fetch and upload maven-metadata.xml from the version directory.
                    # This helps CodeArtifact mark the version as Published for standard Maven packages.
                    # For packages that only have a .pom (e.g. Gradle plugin markers), the
                    # maven-metadata.xml may not exist at the version level — in that case we
                    # skip the upload and rely solely on update_package_versions_status below.
                    meta_uri = (
                        uri_formatted.removesuffix(uri_formatted.split("/")[-1])
                        + "maven-metadata.xml"
                    )
                    # Try to fetch maven-metadata.xml and upload it to CodeArtifact.
                    # First try the version-level path (standard Maven layout):
                    #   <repo>/<package>/<version>/maven-metadata.xml
                    # If not found, try the package-level path (Gradle plugin marker layout):
                    #   <repo>/<package>/maven-metadata.xml
                    # Uploading a 404 HTML page would corrupt the package state, so we
                    # check existence via the Artifactory storage API before fetching.
                    meta_found = False
                    for meta_api_path in [
                        f"/api/storage/{package['repository']}/{package['package']}/{package['version']}/maven-metadata.xml",
                        f"/api/storage/{package['repository']}/{package['package']}/maven-metadata.xml",
                    ]:
                        try:
                            artifactory.artifactory_http_call(args, meta_api_path, raise_on_error=True)
                            # File exists — derive the download URL and fetch it
                            meta_download_uri = meta_api_path.replace("/api/storage/", "")
                            # Build full Artifactory URL
                            if args.artifactoryprefix:
                                prefix = f"/{args.artifactoryprefix}"
                            else:
                                prefix = ""
                            meta_full_uri = f"{args.artifactoryprotocol}://{args.artifactoryhost}{prefix}/{meta_download_uri}"
                            artifactory.artifactory_binary_fetch(
                                args, meta_full_uri, replication_path, foldername
                            )
                            codeartifact.codeartifact_upload_binary(
                                args,
                                client,
                                token_codeartifact,
                                package,
                                tree + "/maven-metadata.xml",
                            )
                            meta_found = True
                            break
                        except Exception as e:
                            logger.debug(f"maven-metadata.xml not found at {meta_api_path}: {e}")
                    if not meta_found:
                        logger.debug(f"No maven-metadata.xml found for {package['package']} {package['version']}, relying on update_package_versions_status")
                    # Always call update_package_versions_status as a failsafe to force Published status
                    codeartifact.codeartifact_update_package_status(
                        args, client, package
                    )
            if publish_fail == True:
                logger.warning(
                    f"Publish for {package['repository']} package {package['package']} version {package['version']} failed, response: {publish_error}"
                )
            if args.dryrun:
                logger.info(f"Dryrun: Would clean up replication folder {tree}")
            else:
                logger.debug(f"Cleaning up {tree} locally on disk")
                try:
                    shutil.rmtree(tree, ignore_errors=True)
                except Exception as exc:
                    logger.warning(f"Exception on deleting {tree}: {exc}")
        # Validation here to confirm package is there in codeartifact after upload
        missing_error = f"Package {package['package']} {package['version']} was not found in codeartifact after upload with status Published. This could mean that your package version did not match semver according to AWS documentation."
        if args.dryrun:
            logger.info(
                f"Dryrun: Would validate package {package['package']} {package['version']} exists in codeartifact."
            )
        else:
            # Retry the check a few times to allow CodeArtifact time to index the package
            check_result = 1
            for _attempt in range(3):
                check_result = codeartifact.codeartifact_check_package_version(args, client, package)
                if check_result == 0:
                    break
                if _attempt < 2:
                    logger.debug(f"Package {package['package']} {package['version']} not yet Published in CodeArtifact, retrying in 5s...")
                    time.sleep(5)
            if check_result != 0:
                logger.warning(missing_error)
                all_packages_published = False
                publish_fail = True
                if publish_error != "":
                    publish_error = publish_error + " -- "
                publish_error = (publish_error + missing_error).replace("'", '"')
            success = False
            if args.cache:
                if publish_fail == True:
                    caching.set_publish_fail(
                        args,
                        package["package"],
                        package["repository"],
                        package["version"],
                        db_file,
                    )
                    all_packages_published == False
                if publish_error != "":
                    caching.set_publish_error(
                        args,
                        package["package"],
                        package["repository"],
                        package["version"],
                        publish_error,
                        db_file,
                    )
                if success == True:
                    logger.debug(
                        f"Cache: Setting package {package['repository']} {package['package']} {package['version']} to codeartifact published"
                    )
                    caching.set_package_version_to_published(
                        args,
                        package["package"],
                        package["repository"],
                        package["version"],
                        db_file,
                    )
    if args.cache:
        if all_packages_published == True:
            caching.set_all_versions_published(
                args, package["package"], package["repository"], db_file
            )
    return {
        "package": package_dict["package"],
        "version": package_dict["version"],
        "published": success,
    }


def replicate_specific_packages(
    args, client, artifactory_repos, codeartifact_repos, db_file
):
    """
    replicate_specific_packages replicates user specified packages. This will
    detect if user specified versions or not. If no versions specified it will
    search for all versions of an artifact name and replicate all versions.

    :param args: command line arguments
    :param client: api client to use with codeartifact
    :artifactory_repos: list of artifactory repo dicts
    :codeartifact_repos: list of current codeartifact repos
    :param db_file: database filename
    """
    package_type = get_package_type(args.repositories, artifactory_repos)
    if package_type not in supported_packages:
        logger.critical(
            f"Repository {args.repositories} package type {package_type} not supported."
        )
        sys.exit(1)
    # Apply repo mapping: use the CodeArtifact repo name for all CodeArtifact operations
    codeartifact_repo = map_repo_name(args.repositories)
    codeartifact.codeartifact_check_create_repo(
        args, client, codeartifact_repo, codeartifact_repos
    )
    # For specified package mode, we are only making a token once instead of refreshing it periodically
    token_codeartifact = client.get_authorization_token(
        domain=args.codeartifactdomain,
        domainOwner=args.codeartifactaccount,
    )["authorizationToken"]
    if args.dryrun:
        endpoint = f"codeartifact-test-endpoint-dryrun.com/{codeartifact_repo}"
        real_endpoint = codeartifact.codeartifact_get_repository_endpoint(
            args, client, codeartifact_repo, package_type
        )
        logger.info(f"Dryrun: true endpoint will be: {real_endpoint}")
    else:
        endpoint = codeartifact.codeartifact_get_repository_endpoint(
            args, client, codeartifact_repo, package_type
        )
    for package in args.packages.split(" "):
        logger.debug(f"Processing package: {package}")
        # Only split on the first colon to preserve Maven-style group/artifact coordinates
        package_split = package.split(":", 1)
        package_name = package_split[0]

        ## See if package is already in cache
        package_check = False
        if args.cache:
            if caching.check_package(args, package_name, args.repositories, db_file):
                logger.info(
                    f"Cache: Package {args.repositories} {package_name} found in cache."
                )
                package_check = True

        # Check to see if packages exist in Artifactory first
        if package_check == False:
            if not artifactory.artifactory_package_search(
                args, package_name, args.repositories
            ):
                logger.critical(
                    f"Specified package {package_name} not found in Artifactory repository {args.repositories}"
                )
                sys.exit(1)
            else:
                logger.info(
                    f"Package {package_name} found in Artifactory repository {args.repositories}"
                )
                if args.cache:
                    if not caching.check_package(
                        args, package_name, args.repositories, db_file
                    ):
                        caching.insert_package(
                            args, package_name, args.repositories, db_file
                        )

        if len(package_split) > 1:
            # Replicate specific version of package
            if len(package_split) > 2:
                logger.critical(f"Malformed package specification, too many ':'")
                sys.exit(1)
            if package_split[1] == "":
                logger.critical(
                    f"You specified a package version with ':' for package {package}. However, you left the version blank."
                )
                sys.exit(1)

            package_dict = {
                "repository": args.repositories,
                "package": package_name,
                "type": package_type,
                "endpoint": endpoint,
            }

            package_dict["version"] = package_split[1]

            package_dict = append_package_specific_keys(args, package_dict)

            if args.cache:
                if not caching.check_version_published(
                    args,
                    package_name,
                    args.repositories,
                    package_dict["version"],
                    db_file,
                ):
                    replicate_package(
                        args, client, token_codeartifact, package_dict, db_file
                    )
                    caching.set_package_version_to_published(
                        args,
                        package_name,
                        package_dict["repository"],
                        package_dict["version"],
                        db_file,
                    )
                else:
                    logger.info(
                        f"Cache: Package {package_dict['repository']} {package_name} version {package_dict['version']} already published, skipping."
                    )
            else:
                replicate_package(
                    args, client, token_codeartifact, package_dict, db_file
                )
        else:
            # Replicate all versions of package
            package_check = False
            # See if all versions of this package were fetched already
            if args.cache:
                if caching.check_all_versions_fetched(
                    args, package, args.repositories, db_file
                ):
                    package_check = True
            if package_check == True:
                logger.info(
                    f"Cache: All versions of package {args.repositories} {package_name} were already fetched."
                )
                if caching.check_all_versions_published(
                    args, package_name, args.repositories, db_file
                ):
                    logger.info(
                        f"Cache: All versions of package {args.repositories} {package_name} were already published, skipping"
                    )
                else:
                    for version in caching.fetch_all_versions(
                        args, package_name, args.repositories, db_file
                    ):
                        if not caching.check_version_published(
                            args, package_name, args.repositories, version, db_file
                        ):
                            package_dict = {
                                "repository": args.repositories,
                                "package": package_name,
                                "type": package_type,
                                "endpoint": endpoint,
                            }
                            package_dict["version"] = version
                            package_dict = append_package_specific_keys(
                                args, package_dict
                            )
                            replicate_package(
                                args, client, token_codeartifact, package_dict, db_file
                            )
            else:
                logger.info(
                    f"Getting all versions for {args.repositories} {package} to populate package dictionary"
                )
                package_dict = {
                    "repository": args.repositories,
                    "package": package_name,
                    "type": package_type,
                    "endpoint": endpoint,
                }
                binaries = artifactory.artifactory_package_binary_search(
                    args, package_dict
                )
                # versions = get_artifactory_package_versions(binaries, package_dict)
                versions = get_filtered_artifactory_package_versions(
                    binaries, package_dict, get_artifactory_version_metadata
                )
                if versions == []:
                    logger.warning(
                        f"No versions of package {args.repositories} {package_dict['package']} were found in Artifactory"
                    )
                logger.info(
                    f"Versions of package {args.repositories} {package_dict['package']} found in Artifactory: {versions}"
                )
                for version in versions:
                    package_dict = {
                        "repository": args.repositories,
                        "package": package_name,
                        "type": package_type,
                        "endpoint": endpoint,
                    }
                    package_dict["version"] = version
                    package_dict = append_package_specific_keys(args, package_dict)
                    replicate_package(
                        args, client, token_codeartifact, package_dict, db_file
                    )


def replicate_all_package_versions(
    args, client, token_codeartifact, packagerepo, db_file
):
    """
    replicate_all_package_versions replicates all versions of a package

    :param args: command line arguments
    :param client: api client to use with codeartifact
    :param  token_codeartifact: the codeartifact authentication token to use
    :packagerepo: dictionary of package and repository
    :param db_file: database filename
    """
    package = packagerepo["package"]
    repository = packagerepo["repository"]
    package_type = packagerepo["package_type"]
    endpoint = packagerepo["endpoint"]

    skip = False
    versions = []
    if args.cache:
        if not args.refresh:
            if caching.check_all_versions_fetched(args, package, repository, db_file):
                versions = caching.fetch_all_versions(
                    args, package, repository, db_file
                )
                skip = True

    if skip == False:
        logger.debug(f"Begin examining package versions: {package}")
        if not artifactory.artifactory_package_search(args, package, repository):
            logger.warning(
                f"Package {package} not found in Artifactory repository {repository}, skipping. This may just be an incorrect parse of the package search return."
            )
        else:
            logger.info(
                f"Package {package} found in Artifactory repository {repository}"
            )
            package_dict = {
                "repository": repository,
                "package": package,
                "type": package_type,
                "endpoint": endpoint,
                "args": args,
            }
            logger.info(
                f"Getting all versions for {package} to populate package dictionary"
            )
            binaries = artifactory.artifactory_package_binary_search(args, package_dict)
            # versions = get_artifactory_package_versions(binaries, package_dict)
            versions = get_filtered_artifactory_package_versions(
                binaries, package_dict, get_artifactory_version_metadata
            )
            # --- Filter versions: last year or last 3 versions ---
            logger.info(f"Filtered versions for {package}: {versions}")
            if args.cache:
                caching.set_all_versions_fetched(args, package, repository, db_file)

    versions_published = True
    if versions == []:
        logger.warning(
            f"No versions of package {package} were found in Artifactory, skipping."
        )
    else:
        logger.debug(f"Versions of package {package}: {versions}")
        for version in versions:
            if args.cache:
                if not caching.check_package_version(
                    args, package, repository, version, db_file
                ):
                    caching.insert_package_version(
                        args, package, repository, version, db_file
                    )
            package_dict = {
                "package": package,
                "version": version,
                "repository": repository,
                "type": package_type,
                "endpoint": endpoint,
            }
            package_dict = append_package_specific_keys(args, package_dict)

            status = replicate_package(
                args, client, token_codeartifact, package_dict, db_file
            )
            if status["published"] == False:
                versions_published = False

    if args.cache:
        if versions_published == True:
            caching.set_all_versions_published(args, package, repository, db_file)

    return versions_published


def replicate_repository(
    args, client, repository, package_type, codeartifact_repos, db_file
):
    """
    replicate_repository replicates an entire specified repository

    :param args: command line arguments
    :param client: api client to use with codeartifact
    :param repository: repository to replicate
    :param package_type: package manager type
    :codeartifact_repos: list of current codeartifact repos
    :param db_file: database filename
    """
    if package_type not in supported_packages:
        logger.warning(
            f"Repository {repository} package type {package_type} not supported."
        )
        return

    # Map Artifactory repo to CodeArtifact repo if mapping exists
    codeartifact_repo = map_repo_name(repository)

    if args.dryrun:
        logger.info(
            f"Dryrun: Would check and create repository {codeartifact_repo} in codeartifact"
        )
        endpoint = f"codeartifact-test-endpoint-dryrun.com/{codeartifact_repo}"
    else:
        codeartifact.codeartifact_check_create_repo(
            args, client, codeartifact_repo, codeartifact_repos
        )
        endpoint = codeartifact.codeartifact_get_repository_endpoint(
            args, client, codeartifact_repo, package_type
        )

    package_list = []

    skip = False

    if args.cache:
        if not caching.check_repository(args, repository, db_file):
            caching.insert_repository(args, repository, db_file)
        failures = caching.fetch_all_packages_with_publish_fail(
            args, repository, db_file
        )
        if failures != []:
            for package in failures:
                publish_error = caching.fetch_error_for_publish_fail(
                    args, package[0], repository, package[1], db_file
                )
                logger.warning(
                    f"Cache: Repository {repository} package {package[0]} "
                    + f"version {package[1]} encountered a publishing error previously: "
                    + f"{publish_error} -- You should fix the package so it can be "
                    + "published and try again with argument --packages specifying the "
                    + "version."
                )
        if caching.check_repository_all_versions_published(args, repository, db_file):
            logger.info(
                f"Cache: Repository {repository} already had all artifacts attempt publishing, skipping."
            )
            return
        if caching.check_repository_all_versions_fetched(args, repository, db_file):
            logger.info(
                f"Cache: Repository {repository} already had all artifacts fetched, using cache."
            )
            package_list = sorted(
                set(caching.fetch_all_packages(args, repository, db_file))
            )
            skip = True

    if skip == False:
        logger.info(f"Generating package list for Artifactory repository {repository}")
        jsondata = artifactory.artifactory_http_call(
            args, f"/api/storage/{repository}?list&deep=1&listFolders=0"
        )

        for file in jsondata["files"]:
            if package_type == "npm":
                # Avoid .npm metadata folders
                if not re.search("^/.npm", file["uri"]):
                    package_name = re.sub("^/", "", file["uri"]).split("/-/")[0]
                    if package_name not in package_list:
                        package_list.append(package_name)
            elif package_type in ["pypi", "maven", "gradle"]:
                if not re.search("maven-metadata.xml", file["uri"]):
                    uri_strip = re.sub("^/", "", file["uri"])
                    logger.debug(f"Processing URI for package extraction: {uri_strip}")
                    uri_list = uri_strip.split("/")
                    repo_name = repository
                    if uri_list and uri_list[0] == repo_name:
                        uri_list = uri_list[1:]  # Remove repo name
                    if package_type == "pypi":
                        # For PyPI: package/version/filename (len > 2)
                        if len(uri_list) > 2:
                            package_name = uri_list[0]
                            if package_name and package_name not in package_list:
                                package_list.append(package_name)
                    else:
                        # For Maven/Gradle: group/artifact/version/filename (len > 3)
                        if len(uri_list) > 3:
                            package_path = uri_list[:-2]
                            package_name = "/".join(package_path)
                            if package_name and package_name not in package_list:
                                package_list.append(package_name)

        package_list = sorted(set(package_list))

        if args.cache:
            for package in package_list:
                if not caching.check_package(args, package, repository, db_file):
                    caching.insert_package(args, package, repository, db_file)

    logger.debug(
        f"Package list to replicate from Artifactory repository {repository}: {package_list}"
    )

    token_codeartifact = client.get_authorization_token(
        domain=args.codeartifactdomain,
        domainOwner=args.codeartifactaccount,
    )["authorizationToken"]
    now = int(time.time())

    proclist = []
    i = 1
    process = False
    n = 1
    versions_published = True

    for package in package_list:
        versions_published = True
        package_dict = {
            "package": package,
            "repository": repository,
            "package_type": package_type,
            "endpoint": endpoint,
        }
        # If user specified only single process, we go right to processing
        if int(args.procs) == 1:
            proclist.append(package_dict)
            process = True
        else:
            # If we reached the end of the list, finish processing
            if n == len(package_list):
                proclist.append(package_dict)
                process = True
            else:
                if i < int(args.procs):
                    proclist.append(package_dict)
                    i = i + 1
                else:
                    proclist.append(package_dict)
                    process = True

        if process == True:
            # Token refresh phase
            if int(time.time()) > now + (token_refresh * 60 * 60):
                token_codeartifact = client.get_authorization_token(
                    domain=args.codeartifactdomain,
                    domainOwner=args.codeartifactaccount,
                )["authorizationToken"]
                now = int(time.time())

            lazy_results = []

            for item in proclist:
                # logger.info(item)
                lazy_result = dask.delayed(replicate_all_package_versions)(
                    args, client, token_codeartifact, item, db_file
                )
                lazy_results.append(lazy_result)

            for status in dask.compute(*lazy_results):
                if status == False:
                    versions_published = False

            i = 1
            process = False
            proclist = []

        n = n + 1

    if args.cache:
        caching.set_repository_all_versions_fetched(args, repository, db_file)
        if versions_published == True:
            caching.set_repository_all_versions_published(args, repository, db_file)


def replicate(args):
    """
    replicate is the main function of the cli dispatch. It sets the codeartifact
    api client, and generates a list of repositories in codeartifact. Then it
    checks Artifactory access and also generates a list of repositories from it.
    We then have a few stages:
      Packages were specified in the command line:
        We run replication specifically for those packages specified.
      If only repositories were specified in command line we replicate those
        specific repositories.
      If neither repositories nor packages specified we replicate all repos.

    :param args: command line arguments
    """

    if not os.path.isdir("./" + replication_path):
        logger.debug(f"Creating directory {replication_path}")
        os.mkdir("./" + replication_path)

    db_file = f"{replication_path}/nodbfile.db"
    if args.cache:
        if args.dynamodb:
            if args.dryrun:
                db_file = f"artifactory-codeartifact-migrator-dryrun"
            else:
                db_file = f"artifactory-codeartifact-migrator-prod"
        else:
            if args.dryrun:
                db_file = f"{replication_path}/acm-dryrun.db"
            else:
                db_file = f"{replication_path}/acm-prod.db"
        caching.check_create_database(args, db_file)
        if args.clean:
            logger.info("Clean was called")
            caching.clean_cache(args, db_file)
            caching.check_create_database(args, db_file)

    aws_config = Config(
        region_name=args.codeartifactregion,
        signature_version="v4",
        retries={"max_attempts": 10, "mode": "standard"},
    )

    client = boto3.client("codeartifact", config=aws_config)

    # This checks codeartifact access and gives us a list of repos to examine
    codeartifact_repos = codeartifact.codeartifact_list_repositories(client)
    logger.debug(f"Codeartifact repo list:\n{codeartifact_repos}")

    # Then we check Artifactory access
    logger.debug("Checking Artifactory access and repository list")
    jsondata = artifactory.artifactory_http_call(args, "/api/storageinfo")
    artifactory_repos = jsondata["repositoriesSummaryList"]
    logger.debug(f"Artifactory repo list:\n{artifactory_repos}")

    if args.packages:
        if args.repositories:
            logger.info("Specific package replication specified")
            if len(args.repositories.split(" ")) != 1:
                logger.critical(
                    "You specified packages to replicate. However you also specified multiple repositories. You can only specify one repository if specifying packages to replicate."
                )
                sys.exit(1)
            else:
                if args.refresh:
                    logger.info(f"Refreshing all packages in {args.repositories}")
                    caching.reset_fetched_packages(args.repositories, db_file)
                check_artifactory_repos(args.repositories, artifactory_repos)
                replicate_specific_packages(
                    args, client, artifactory_repos, codeartifact_repos, db_file
                )
        else:
            logger.critical(
                "You specified packages to replicate. However you didn't specify a repository."
            )
            sys.exit(1)
    elif args.repositories:
        logger.info("Specific repository replication specified")
        check_artifactory_repos(args.repositories, artifactory_repos)
        for repository in args.repositories.split(" "):
            if args.refresh:
                logger.info(f"Refreshing all packages in {repository}")
                caching.reset_fetched_packages(args, repository, db_file)
            package_type = get_package_type(repository, artifactory_repos)
            replicate_repository(
                args, client, repository, package_type, codeartifact_repos, db_file
            )
    else:
        logger.info("All repository replication specified")
        for repo in artifactory_repos:
            repository = repo["repoKey"]
            if repository != "TOTAL":
                if repo["repoType"] == "LOCAL":
                    if args.refresh:
                        logger.info(f"Refreshing all packages in {repository}")
                        caching.reset_fetched_packages(args, repository, db_file)
                    package_type = get_package_type(repository, artifactory_repos)
                    replicate_repository(
                        args,
                        client,
                        repository,
                        package_type,
                        codeartifact_repos,
                        db_file,
                    )

    if args.dryrun:
        logger.info("Dryrun operations completed")
