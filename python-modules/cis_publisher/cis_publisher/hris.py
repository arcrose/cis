import cis_profile
import cis_publisher
import boto3
import botocore
import os
import logging
import json
import time
import requests
from datetime import datetime, timezone, timedelta
from traceback import format_exc

logger = logging.getLogger(__name__)


class HRISPublisher:
    def __init__(self, context={}):
        self.secret_manager = cis_publisher.secret.Manager()
        self.context = context
        self.report = None

    def get_s3_cache(self):
        """
        If cache exists and is not older than timedelta() then return it, else don't
        return: dict JSON
        """
        s3 = boto3.client("s3")
        bucket = os.environ.get("CIS_BUCKET_URL")
        cache_time = int(os.environ.get("CIS_HRIS_CACHE_TIME_HOURS")) or 1
        recent = datetime.now(timezone.utc) - timedelta(hours=cache_time)
        try:
            objects = s3.list_objects_v2(Bucket=bucket)
            # bucket has zero contents?
            if "Contents" not in objects:
                logger.info("No S3 cache present")
                return None
            # Recent file?
            for o in objects["Contents"]:
                if o["Key"] == "cache.json" and o["LastModified"] < recent:
                    logger.info("S3 cache too old, not using")
                    return None
            response = s3.get_object(Bucket=bucket, Key="cache.json")
            data = response["Body"].read()
        except botocore.exceptions.ClientError as e:
            logger.error("Could not find S3 cache file: {}".format(e))
            return None
        logger.info("Using S3 cache")
        return json.loads(data)

    def save_s3_cache(self, data):
        """
        @data dict JSON
        """
        s3 = boto3.client("s3")
        bucket = os.environ.get("CIS_BUCKET_URL")
        s3.put_object(Bucket=bucket, Key="cache.json", Body=json.dumps(data))
        logger.info("Wrote S3 cache file")

    def publish(self, user_ids=None, chunk_size=30):
        """
        Glue to create or fetch cis_profile.User profiles for this publisher
        Then pass everything over to the Publisher class
        None, ALL profiles are sent.
        @user_ids: list of str - user ids to publish. If None, all users are published.
        @chunk_size: int when no user_id is selected, this is the size of the chunk/slice we'll create to divide the
        work between function calls (to self)
        """
        if user_ids is None:
            le = "All"
        else:
            le = len(user_ids)
        logger.info("Starting HRIS Publisher [{} users]".format(le))
        cache = self.get_s3_cache()

        # Get access to the known_users function first
        # We override profiles from `publisher` object later on
        publisher = cis_publisher.Publish([], login_method="ad", publisher_name="hris")
        if cache is None:
            publisher.get_known_cis_users()
            publisher.get_known_cis_users_paginated()
            self.fetch_report()
            self.save_s3_cache(
                {
                    "all_known_profiles": publisher.all_known_profiles,
                    "known_cis_users": publisher.known_cis_users,
                    "report": self.report,
                }
            )
        else:
            publisher.all_known_profiles = cache["all_known_profiles"]
            publisher.known_cis_users = cache["known_cis_users"]
            for u in publisher.known_cis_users:
                publisher.known_cis_users_by_user_id[u["user_id"]] = u["primary_email"]
                publisher.known_cis_users_by_email[u["primary_email"]] = u["user_id"]
            self.report = cache["report"]

        # Should we fan-out processing to multiple function calls?
        if user_ids is None:
            self.fan_out(publisher, chunk_size)
        else:
            self.process(publisher, user_ids)

    def deactivate_users(self, publisher, profiles, report):
        """
        Deactivate users present in CIS but not in HRIS
        @publisher cis_publisher.Publisher object
        @profiles list of cis_profile.User that were converted
        @report HRIS report
        """
        user_ids_in_hris = []
        for hruser in report.get("Report_Entry"):
            hruser_work_email = hruser.get("PrimaryWorkEmail").lower()
            try:
                current_user_id = publisher.known_cis_users_by_email[hruser_work_email]
            except KeyError:
                logger.critical(
                    "Repeated: There is no user_id in CIS Person API for HRIS User:{}".format(hruser_work_email)
                )
                continue
            user_ids_in_hris.append(current_user_id)

        delta = set(publisher.known_cis_users_by_user_id.keys()) - set(user_ids_in_hris)

        # XXX this is a slow work-around to figure out who is staff
        # This data should eventually be directly queriable from Person API with a filter (this does not currently
        # exist)
        user_ids_to_deactivate = []
        for potential_user_id in delta:
            if potential_user_id in publisher.all_known_profiles:
                profile = publisher.all_known_profiles[potential_user_id]
                # Convert as needed to a dict
                try:
                    profile = profile.as_dict()
                except (TypeError, AttributeError):
                    pass
                # Check if it has the attributes
                try:
                    ldap_groups = profile["access_information"]["ldap"]["values"]
                    is_staff = profile["staff_information"]["staff"]["value"]
                # Not a staff user f these fields don't exist
                except KeyError:
                    logger.debug(
                        "staff_information.staff or access_information.ldap fields are null,"
                        " won't deactivate {}".format(potential_user_id)
                    )
                    continue

                if (is_staff is True) or (
                    is_staff is False and (ldap_groups is not None and "admin_accounts" not in ldap_groups)
                ):
                    user_ids_to_deactivate.append(potential_user_id)

        if len(user_ids_to_deactivate) > 0:
            logger.info(
                "Will deactivate {} users because they're in CIS but not in HRIS".format(user_ids_to_deactivate)
            )
            for user in user_ids_to_deactivate:
                logger.info("User selected for deactivation: {}".format(user))
                # user from cis
                try:
                    p = cis_profile.User(publisher.all_known_profiles[user])
                except TypeError:
                    p = publisher.all_known_profiles[user]

                # our partial update
                newp = cis_profile.User()
                newp.user_id = p.user_id
                newp.primary_email = p.primary_email
                newp.active.value = False
                newp.active.signature.publisher.name = "hris"
                try:
                    newp.sign_attribute("active", publisher_name="hris")
                except Exception as e:
                    logger.critical(
                        "Profile data signing failed for user {} - skipped signing, verification "
                        "WILL FAIL ({})".format(newp.primary_email.value, e)
                    )
                    logger.debug("Profile data {}".format(newp.as_dict()))

                profiles.append(newp)
        return profiles

    def process(self, publisher, user_ids):
        """
        Process profiles and post them
        @publisher object the publisher object to operate on
        @user_ids list of user ids to process in this batch
        """
        report_profiles = self.fetch_report()
        profiles = self.convert_hris_to_cis_profiles(
            report_profiles, publisher.known_cis_users_by_user_id, publisher.known_cis_users_by_email, user_ids
        )
        profiles = self.deactivate_users(publisher, profiles, report_profiles)
        del report_profiles

        logger.info("Processing {} profiles".format(len(profiles)))
        publisher.profiles = profiles

        failures = []
        try:
            failures = publisher.post_all(user_ids=user_ids)
        except Exception as e:
            logger.error("Failed to post_all() HRIS profiles. Trace: {}".format(format_exc()))
            raise e

        if len(failures) > 0:
            logger.error("Failed to post {} profiles: {}".format(len(failures), failures))

    def fan_out(self, publisher, chunk_size):
        """
        Splices all users to process into chunks
        and self-invoke as many times as needed to complete all work in parallel lambda functions
        When self-invoking, this will effectively call self.process() instead of self.fan_out()
"]
        Note: chunk_size should never result in the invoke() argument to exceed 128KB (len(Payload.encode('utf-8') <
        128KB) as this is the maximum AWS Lambda payload size.

        @publisher object the cis_publisher object to operate on
        @chunk_size int size of the chunk to process
        """
        all_user_ids = []
        report_profiles = self.fetch_report()

        for u in report_profiles.get("Report_Entry"):
            try:
                all_user_ids.append(publisher.known_cis_users_by_email[u.get("PrimaryWorkEmail")])
            except KeyError:
                logger.critical(
                    "There is no user_id in CIS Person API for HRIS User {}."
                    "This user may not be created in HRIS yet?".format(u.get("PrimaryWorkEmail"))
                )
                continue
        sliced = [all_user_ids[i : i + chunk_size] for i in range(0, len(all_user_ids), chunk_size)]
        logger.info(
            "No user_id selected. Creating slices of work, chunck size: {}, slices: {}, total users: {} and "
            "faning-out work to self".format(chunk_size, len(sliced), len(all_user_ids))
        )
        lambda_client = boto3.client("lambda")
        for s in sliced:
            lambda_client.invoke(FunctionName=self.context.function_name, InvocationType="Event", Payload=json.dumps(s))
            time.sleep(3)  # give api calls a chance, otherwise this storms resources

        logger.info("Exiting slicing function successfully")

    def fetch_report(self):
        """
        Fetches the Workday report data
        Strip out unused data
        Returns the JSON document with only used data
        """
        if self.report is not None:
            return self.report

        hris_url = self.secret_manager.secret("hris_url")
        hris_username = self.secret_manager.secret("hris_user")
        hris_password = self.secret_manager.secret("hris_password")

        logger.info("Fetching HRIS report from {}".format(hris_url))
        params = dict(format="json")

        if os.environ.get("CIS_ENVIRONMENT") == "development":
            logger.debug("Dev environment, not using credentials")
            res = requests.get(hris_url, params=params)
        else:
            res = requests.get(hris_url, auth=requests.auth.HTTPBasicAuth(hris_username, hris_password), params=params)

        del hris_password
        del hris_username

        if not res.ok:
            logger.error(
                "Error fetching the HRIS report, status_code: {}, reason: {}, text: {}".format(
                    res.status_code, res.reason, res.text
                )
            )
            raise ValueError("Could not fetch HRIS report")
        self.report = res.json()
        return self.report

    def dedup_changes(self, publisher, profiles):
        """
        Remove profiles that have no HRIS changes so that we don't send them for no reason
        XXX this function is not currently used (check!)

        @publisher cis_publisher.Publisher object
        @profiles list of cis_profile.User
        """
        dedup = []
        for p in profiles:
            user_id = publisher.known_cis_users_by_email[p.primary_email.value]
            if user_id is None:
                dedup.append(p)
                continue
            cis_p = cis_profile.User(publisher.all_known_profiles[user_id])
            if p.timezone.value is not None:
                if (
                    (p.staff_information == cis_p.staff_information)
                    and (p.access_information.hris == cis_p.access_information.hris)
                    and (p.active == cis_p.active)
                ):
                    continue
            dedup.append(p)
        return dedup

    def convert_hris_to_cis_profiles(self, hris_data, cis_users_by_user_id, cis_users_by_email, user_ids):
        """
        @hris_data list dict of HRIS data
        @cis_users_by_user_id dict of Person API known users by user_id=>email
        @cis_users_by_email dict of Person API known users by email=>user_id to convert to user_ids
        @user_ids list of user ids to convert

        returns: list of cis_profile.Profile
        """

        def tz_convert(hris_tz):
            tzmap = {
                "GMT United Kingdom Time (London)": "UTC+0000 Europe/London",
                "GMT Western European Time (Casablanca)": "UTC+0100 Africa/Casablanca",
                "GMT+01:00 Central European Time (Amsterdam)": "UTC+0200 Europe/Amsterdam",
                "GMT+01:00 Central European Time (Berlin)": "UTC+01:00 Europe/Berlin",
                "GMT+01:00 Central European Time (Oslo)": "UTC+01:00 Europe/Oslo",
                "GMT+01:00 Central European Time (Paris)": "UTC+01:00 Europe/Paris",
                "GMT+01:00 Central European Time (Prague)": "UTC+01:00 Europe/Prague",
                "GMT+01:00 Central European Time (Stockholm)": "UTC+01:00 Europe/Stockholm",
                "GMT+02:00 Eastern European Time (Athens)": "UTC+02:00 Europe/Athens",
                "GMT+02:00 Eastern European Time (Bucharest)": "UTC+02:00 Europe/Bucharest",
                "GMT+02:00 Eastern European Time (Helsinki)": "UTC+02:00 Europe/Helsinki",
                "GMT+02:00 South Africa Standard Time (Johannesburg)": "UTC+02:00 Africa/Johannesburg",
                "GMT+03:00 East Africa Time (Nairobi)": "UTC+03:00 Africa/Nairobi",
                "GMT+03:00 Moscow Standard Time (Moscow)": "UTC+03:00 Europe/Moscow",
                "GMT+05:00 Pakistan Standard Time (Karachi)": "UTC+05:00 Pakistan/Karachi",
                "GMT+05:30 India Standard Time (Kolkata)": "UTC+05:30 Asia/Kolkata",
                "GMT+07:00 Western Indonesia Time (Jakarta)": "UTC+07:00 Asia/Jakarta",
                "GMT+08:00 Australian Western Standard Time (Perth)": "UTC+08:00 Australia/Perth",
                "GMT+08:00 China Standard Time (Shanghai)": "UTC+08:00 Asia/Shanghai",
                "GMT+08:00 Taipei Standard Time (Taipei)": "UTC+08:00 Asia/Taipei",
                "GMT+09:00 Japan Standard Time (Tokyo)": "UTC+09:00 Asia/Tokyo",
                "GMT+10:00 Australian Eastern Standard Time (Brisbane)": "UTC+10:00 Australia/Brisbane",
                "GMT+12:00 New Zealand Time (Auckland)": "UTC+12:00 Pacific/Auckland",
                "GMT-03:00 Argentina Standard Time (Buenos Aires)": "UTC-0300 America/Buenos_Aires",
                "GMT-03:00 Brasilia Standard Time (Recife)": "UTC-0300 America/Recife",
                "GMT-04:00 Atlantic Time (Halifax)": "UTC-0400 America/Halifax",
                "GMT-05:00 Eastern Time": "UTC-0500 US/Eastern",
                "GMT-06:00 Central Standard Time (Regina)": "UTC-0600 America/Regina",
                "GMT-06:00 Central Time (Chicago)": "UTC-0600 America/Chicago",
                "GMT-06:00 Central Time": "UTC-0600 US/Central",
                "GMT-07:00 Mountain Time": "UTC-0700 US/Mountain",
                "GMT-08:00 Pacific Time (Los Angeles)": "UTC-0800 America/Los_Angeles",
                "GMT-08:00 Pacific Time (Tijuana)": "UTC-0800 America/Tijuana",
                "GMT-08:00 Pacific Time": "UTC-0800 US/Pacific",
            }
            try:
                tzmap[hris_tz]
            except KeyError:
                logger.warning(
                    "Unknown timezone in workday, defaulting to UTC. Timezone from HRIS was" " {}.".format(hris_tz)
                )
                return "UTC+0000 Europe/London"
            return tzmap[hris_tz]

        def cost_center_convert(cc):
            """
            Cost centers can have decimal points
            So it's a float
            """
            return str(float(cc.split(" ")[0]))

        def strbool_convert(v):
            return v.lower() in ("yes", "true", "t", "1")

        # Convert
        user_array = []
        for hruser in hris_data.get("Report_Entry"):
            hruser_work_email = hruser.get("PrimaryWorkEmail").lower()
            logger.debug("filtering fields for user email {}".format(hruser_work_email))

            # NOTE:
            # The HRIS setup will DELETE users when they need to be deactivated and removed. It will set INACTIVE when
            # users may not yet be provisioned. This is important as if this changes the code here would have to also
            # change.
            hruser_active_state = int(hruser.get("CurrentlyActive"))
            if hruser_active_state == 0:
                logger.debug(
                    "User {} is currently set to inactive in HRIS Report, skipping integration".format(
                        hruser_work_email
                    )
                )
                continue

            current_user_id = None
            try:
                current_user_id = cis_users_by_email[hruser_work_email]
            except KeyError:
                logger.critical(
                    "There is no user_id in CIS Person API for HRIS User {}."
                    " This user may not be created by HRIS yet?".format(hruser_work_email)
                )
                continue
            user_ids_lower_case = [x.lower() for x in user_ids]

            if current_user_id.lower() not in user_ids_lower_case:
                # Skip this user, it's not in the list requested to convert
                logger.debug("skipping user {}, not in requested conversion list".format(current_user_id))
                continue

            p = cis_profile.User()
            # Note: Never use non-preferred names here
            #            p.last_name.value = hruser.get("Preferred_Name_-_Last_Name")
            #            p.last_name.signature.publisher.name = "hris"
            #            p.first_name.value = hruser.get("PreferredFirstName")
            #            p.first_name.signature.publisher.name = "hris"
            p.active.value = True
            p.active.signature.publisher.name = "hris"

            p.primary_email.value = hruser_work_email
            p.primary_email.signature.publisher.name = "hris"

            p.timezone.value = tz_convert(hruser.get("Time_Zone"))
            p.timezone.signature.publisher.name = "hris"
            p.timezone.metadata.display = "staff"

            p.staff_information.manager.value = strbool_convert(hruser.get("IsManager"))
            p.staff_information.manager.signature.publisher.name = "hris"
            p.staff_information.manager.metadata.display = "staff"

            p.staff_information.director.value = strbool_convert(hruser.get("isDirectorOrAbove"))
            p.staff_information.director.signature.publisher.name = "hris"
            p.staff_information.director.metadata.display = "staff"
            if len(hruser.get("EmployeeID")) > 0:
                p.staff_information.staff.value = True
            else:
                p.staff_information.staff.value = False
            p.staff_information.staff.signature.publisher.name = "hris"
            p.staff_information.staff.metadata.display = "staff"

            p.staff_information.title.value = hruser.get("businessTitle")
            p.staff_information.title.signature.publisher.name = "hris"
            p.staff_information.title.metadata.display = "staff"

            p.staff_information.team.value = hruser.get("Team")
            p.staff_information.team.signature.publisher.name = "hris"
            p.staff_information.team.metadata.display = "staff"

            p.staff_information.cost_center.value = cost_center_convert(hruser.get("Cost_Center"))
            p.staff_information.cost_center.signature.publisher.name = "hris"
            p.staff_information.cost_center.metadata.display = "staff"

            p.staff_information.worker_type.value = hruser.get("WorkerType")
            p.staff_information.worker_type.signature.publisher.name = "hris"
            p.staff_information.worker_type.metadata.display = "staff"

            p.staff_information.wpr_desk_number.value = hruser.get("WPRDeskNumber")
            p.staff_information.wpr_desk_number.signature.publisher.name = "hris"
            p.staff_information.wpr_desk_number.metadata.display = "staff"

            p.staff_information.office_location.value = hruser.get("LocationDescription")
            p.staff_information.office_location.signature.publisher.name = "hris"
            p.staff_information.office_location.metadata.display = "staff"

            p.access_information.hris["values"] = {}
            p.access_information.hris.signature.publisher.name = "hris"
            p.access_information.hris["values"]["employee_id"] = hruser.get("EmployeeID")
            p.access_information.hris["values"]["worker_type"] = hruser.get("WorkerType")
            p.access_information.hris["values"]["primary_work_email"] = hruser_work_email
            p.access_information.hris["values"]["managers_primary_work_email"] = hruser.get(
                "Worker_s_Manager_s_Email_Address"
            )
            p.access_information.hris["values"]["egencia_pos_country"] = hruser.get("EgenciaPOSCountry")
            try:
                p.sign_all(publisher_name="hris")
            except Exception as e:
                logger.critical(
                    "Profile data signing failed for user {} - skipped signing, verification "
                    "WILL FAIL ({})".format(p.primary_email.value, e)
                )
                logger.debug("Profile data {}".format(p.as_dict()))
            try:
                p.validate()
            except Exception as e:
                logger.critical(
                    "Profile schema validation failed for user {} - skipped validation, verification "
                    "WILL FAIL({})".format(p.primary_email.value, e)
                )
                logger.debug("Profile data {}".format(p.as_dict()))

            try:
                p.verify_all_publishers(cis_profile.User())
            except Exception as e:
                logger.critical(
                    "Profile publisher verification failed for user {} - skipped signing, verification "
                    "WILL FAIL ({})".format(p.primary_email.value, e)
                )
                logger.debug("Profile data {}".format(p.as_dict()))

            logger.info("Processed (signed and verified) HRIS report's user {}".format(p.primary_email.value))
            user_array.append(p)
        return user_array
