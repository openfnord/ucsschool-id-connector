# -*- coding: utf-8 -*-

# Copyright 2019-2020 Univention GmbH
#
# http://www.univention.de/
#
# All rights reserved.
#
# The source code of this program is made available
# under the terms of the GNU Affero General Public License version 3
# (GNU AGPL V3) as published by the Free Software Foundation.
#
# Binary versions of this program provided by Univention to you as
# well as other copyrighted, protected or trademarked materials like
# Logos, graphics, fonts, specific documentations and configurations,
# cryptographic keys etc. are subject to a license agreement between
# you and Univention and not subject to the GNU AGPL V3.
#
# In the case you use this program under the terms of the GNU AGPL V3,
# the program is provided in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public
# License with the Debian GNU/Linux or Univention distribution in file
# /usr/share/common-licenses/AGPL-3; if not, see
# <http://www.gnu.org/licenses/>.

import abc
import datetime
import random
import string
from collections import defaultdict
from typing import Any, Callable, Dict, List, Tuple, Type

from async_property import async_property

from ucsschool_id_connector.constants import API_SCHOOL_CACHE_TTL
from ucsschool_id_connector.models import (
    ListenerActionEnum,
    ListenerObject,
    ListenerUserAddModifyObject,
    ListenerUserOldDataEntry,
    ListenerUserRemoveObject,
    SchoolAuthorityConfiguration,
    SchoolUserRole,
    UnknownSchoolUserRole,
    UserPasswords,
)
from ucsschool_id_connector.plugins import hook_impl
from ucsschool_id_connector.requests import APICommunicationError
from ucsschool_id_connector.utils import (
    ConsoleAndFileLogging,
    get_source_uid,
    recursive_dict_update,
    school_class_dn_regex,
)

BB_API_MAIN_ATTRIBUTES = {
    "name",
    "birthday",
    "disabled",
    "email",
    "firstname",
    "lastname",
    "password",
    "record_uid",
    "roles",
    "school",
    "school_classes",
    "schools",
    "source_uid",
    "ucsschool_roles",
}


class ConfigurationError(Exception):
    pass


class MissingData(Exception):
    pass


class SkipAttribute(Exception):
    pass


class UnknownSchool(Exception):
    def __init__(self, *args, school: str, **kwargs):
        self.school = school
        super().__init__(*args, **kwargs)


class PerSchoolAuthorityUserHandlerBase(abc.ABC):
    """
    Base class for plugins handling user objects, per school authority code.

    The plugin entry code is in the class `UserHandlerPluginBase`.
    """

    _password_attributes = set(UserPasswords.__fields__.keys())
    school_role_to_api_role = {
        SchoolUserRole.staff: "staff",
        SchoolUserRole.student: "student",
        SchoolUserRole.teacher: "teacher",
    }

    def __init__(self, school_authority: SchoolAuthorityConfiguration, plugin_name: str):
        self.school_authority = school_authority
        self.plugin_name = plugin_name
        self.logger = ConsoleAndFileLogging.get_logger(
            f"{self.__class__.__name__}({self.school_authority.name})"
        )
        self._roles_on_target_cache: Dict[str, str] = {}
        self._school_ids_on_target_cache: Dict[str, str] = {}
        self._school_ids_on_target_cache_creation = datetime.datetime(1970, 1, 1)
        self.class_dn_regex = school_class_dn_regex()

    async def handle_create_or_update(self, obj: ListenerUserAddModifyObject) -> None:
        """Create or modify user."""
        self.logger.info("Going to create or update %r.", obj)
        self.logger.debug("*** obj.dict()=%r", obj.dict())
        if not await self.user_has_schools(obj):
            return
        await self.print_users_ids(obj)
        try:
            request_body = await self.map_attributes(obj)
        except Exception as exc:
            self.logger.exception("Mapping attributes: %s", exc)
            raise
        self.logger.debug("*** request_body=%r", request_body)
        await self.do_create_or_update(request_body)

    async def handle_remove(self, obj: ListenerUserRemoveObject) -> None:
        """Remove user."""
        self.logger.info("Going to remove %r.", obj)
        self.logger.debug("*** obj.dict()=%r", obj.dict())
        await self.do_remove(obj)

    async def user_has_schools(self, obj: ListenerUserAddModifyObject) -> bool:
        """
        Delete user in school authority if it has no more schools in this
        school authority.
        """
        api_schools = await self.schools_ids_on_target
        current_schools = [s for s in obj.schools if s in api_schools]
        if not current_schools:
            await self.handle_has_no_schools(obj)
            return False
        return True

    async def print_users_ids(self, obj: ListenerUserAddModifyObject) -> None:
        """
        Print info about its `schools`, `record_uid` and `source_uid`.
        """
        schools_ids = await self.schools_ids_on_target
        if obj.old_data:
            old_record_uid = obj.old_data.record_uid
            old_source_uid = obj.old_data.source_uid
            old_schools = [s for s in obj.old_data.schools if s in schools_ids.keys()]
        else:
            old_record_uid = old_source_uid = old_schools = "<no old_data>"
        self.logger.debug(
            "User %r has old->new schools=(%r->%r) record_uid=(%r->%r) " "source_uid=(%r->%r).",
            obj.username,
            old_schools,
            [s for s in obj.schools if s in schools_ids],
            old_record_uid,
            obj.record_uid,
            old_source_uid,
            obj.source_uid,
        )

    async def handle_has_no_schools(self, obj: ListenerUserAddModifyObject) -> None:
        """Delete user without schools in this school authority."""
        self.logger.info(
            "All schools of user %r in this school authority (%r) have been "
            "removed. Deleting user from school authority...",
            obj.username,
            self.school_authority.name,
        )
        if obj.old_data:
            self.logger.debug(
                "User %r has 'old_data': schools=%r record_uid=%r source_uid=%r",
                obj.old_data.schools,
                obj.old_data.record_uid,
                obj.old_data.source_uid,
            )
            old_data = obj.old_data
        else:
            self.logger.debug("User %r has no 'old_data'.")
            self.logger.debug(
                "User %r has currently: schools=%r record_uid=%r source_uid=%r",
                obj.username,
                obj.schools,
                obj.record_uid,
                obj.source_uid,
            )
            old_data = ListenerUserOldDataEntry(
                record_uid=obj.record_uid, source_uid=obj.source_uid, schools=obj.schools
            )
        remove_obj = ListenerUserRemoveObject(
            dn=obj.dn,
            id=obj.id,
            udm_object_type=obj.udm_object_type,
            action=ListenerActionEnum.delete,
            old_data=old_data,
        )
        await self.do_remove(remove_obj)

    @async_property
    async def schools_ids_on_target(self) -> Dict[str, str]:
        """
        Schools known by the target system dict(name -> ID).

        (ID is in REST APIs usually a URL).
        """
        # update list of school URLs
        if not self._school_ids_on_target_cache or (
            self._school_ids_on_target_cache_creation + datetime.timedelta(seconds=API_SCHOOL_CACHE_TTL)
            < datetime.datetime.now()
        ):
            await self.refresh_schools()
            self._school_ids_on_target_cache_creation = datetime.datetime.now()
        return self._school_ids_on_target_cache

    async def refresh_schools(self):
        self._school_ids_on_target_cache.clear()
        self._school_ids_on_target_cache.update(await self.fetch_schools())
        self.logger.debug(
            "Schools known by API server: %s",
            ", ".join(self._school_ids_on_target_cache.keys()),
        )

    async def fetch_schools(self) -> Dict[str, str]:
        """
        Fetch all schools from API of school authority.

        Something like:
            return dict((school["name"], school["url"]) for school in json_resp["results"])

        :return: dict school name -> url
        """
        raise NotImplementedError()

    @async_property
    async def roles_on_target(self) -> Dict[str, str]:
        """
        Roles known by the target system dict(name -> ID).

        (ID is in REST APIs usually a URL).
        """
        if not self._roles_on_target_cache:
            await self.refresh_roles()
        return self._roles_on_target_cache

    async def refresh_roles(self):
        self._roles_on_target_cache.clear()
        self._roles_on_target_cache.update(await self.fetch_roles())
        self.logger.debug("Roles known by API server: %s", ", ".join(self._roles_on_target_cache.keys()))

    async def fetch_roles(self) -> Dict[str, str]:
        """
        Fetch all roles from API of school authority.

        Something like:
            return dict((role["name"], role["url"]) for role in json_resp["results"])

        :return: dict role name -> url
        """
        raise NotImplementedError()

    async def do_create_or_update(self, request_body: Dict[str, Any]) -> None:
        exists, api_user_data = await self.user_exists_on_target(request_body)
        if exists:
            self.logger.info("User exists on target system, modifying it.")
            await self.do_modify(request_body, api_user_data)
        else:
            self.logger.info("User does not exist on target system, creating it.")
            await self.do_create(request_body, api_user_data)

    async def user_exists_on_target(self, request_body: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
        """
        Check if the user exists on the school authorities system.

        :param dict request_body: output of `map_attributes`
        :return: tuple(bool, dict) indicating if the user exists, and a
            (possibly empty) dict that will be passed to `_do_create` or
            `_do_modify`.
        """
        raise NotImplementedError()

    async def do_create(self, request_body: Dict[str, Any], api_user_data: Dict[str, Any]) -> None:
        """
        Create a user object at the target.

        :param dict request_body: output of `map_attributes`
        :param dict api_user_data: output of `user_exists_on_target`
        """
        raise NotImplementedError()

    async def do_modify(self, request_body: Dict[str, Any], api_user_data: Dict[str, Any]) -> None:
        """
        Modify a user object at the target.

        :param dict request_body: output of `map_attributes`
        :param dict api_user_data: output of `user_exists_on_target`
        """
        raise NotImplementedError()

    async def do_remove(self, obj: ListenerUserRemoveObject) -> None:
        """Delete a user object at the target."""
        raise NotImplementedError()

    async def map_attributes(self, obj: ListenerUserAddModifyObject) -> Dict[str, Any]:
        """Create dict representing the user."""
        res = {}
        # set attributes configured in mapping
        for key_here, key_there in self.school_authority.mapping.items():
            _handle_attr_method_name = f"_handle_attr_{key_here}"
            if hasattr(self, _handle_attr_method_name):
                # handling of special attributes: try using a _handle_attr_* method
                meth: Callable[[ListenerUserAddModifyObject], Any] = getattr(
                    self, _handle_attr_method_name
                )
                try:
                    value_here = await meth(obj)
                except SkipAttribute:
                    continue
            else:
                # no such method, use value from listener file directly
                value_here = obj.object.get(key_here)

            # `none` may be invalid for the school authorities API
            if value_here is None:
                try:
                    value_here = self._handle_none_value(key_here)
                except SkipAttribute:
                    continue

            recursive_dict_update(res, self._update_for_mapping_data(key_here, key_there, value_here))

        recursive_dict_update(res, self._handle_password_hashes(obj))
        return res

    @staticmethod
    async def _handle_attr_disabled(obj: ListenerUserAddModifyObject) -> bool:
        """Pass on state of 'disabled'."""
        return obj.object["disabled"] == "1"

    async def _handle_attr_password(self, obj: ListenerUserAddModifyObject) -> str:
        """Generate a random password, unless password hashes are to be sent."""
        if self.school_authority.plugin_configs[self.plugin_name].get("passwords_target_attribute"):
            self.logger.warning(
                "'passwords_target_attribute' is set, please remove 'password' from 'mapping'. Not "
                "sending value for 'password'."
            )
            raise SkipAttribute()

        pw = list(string.ascii_letters + string.digits + ".-_")
        random.shuffle(pw)
        return "".join(pw[:15])

    async def _handle_attr_roles(self, obj: ListenerUserAddModifyObject) -> List[str]:
        """
        `roles` attribute of UCS@school users is determined by their
        objectClasses / UDM options. Return URLs of ucsschool role in servers
        API.
        """
        try:
            api_roles = (self.school_role_to_api_role[role] for role in obj.school_user_roles)
        except KeyError:
            raise UnknownSchoolUserRole(
                f"Role unknown in internal mapping: {obj.school_user_roles!r}.",
                roles=[role.name for role in obj.school_user_roles],
            )
        return [(await self.roles_on_target)[role] for role in api_roles]

    async def _handle_attr_school(self, obj: ListenerUserAddModifyObject) -> str:
        """
        Get URL of primary school for this user.
        """
        target_schools = await self.schools_ids_on_target
        schools = sorted(set([obj.school] + obj.schools))
        for school in schools:
            try:
                return target_schools[school]
            except KeyError:
                self.logger.warning("Ignoring unknown OU %r in 'school[s]' of %r.", school, obj)
        else:
            raise UnknownSchool(
                f"None of the users schools ({schools!r}) are known on the target server.",
                school=obj.school,
            )

    async def _handle_attr_schools(self, obj: ListenerUserAddModifyObject) -> List[str]:
        """
        Get URLs of all schools in our school authority that the user is
        currently a member of.
        """
        res = []
        api_schools_cache = await self.schools_ids_on_target
        schools = sorted(set([obj.school] + obj.schools))
        for school in schools:
            try:
                res.append(api_schools_cache[school])
            except KeyError:
                self.logger.warning("Ignoring unknown OU %r in 'school[s]' of %r.", school, obj)
        if res:
            return res
        else:
            raise UnknownSchool(
                f"None of the users schools ({schools!r}) are known on the target server.",
                school=obj.school,
            )

    async def _handle_attr_school_classes(
        self, obj: ListenerUserAddModifyObject
    ) -> Dict[str, List[str]]:
        """Get school classes the user is in this school authority."""
        known_schools = (await self.schools_ids_on_target).keys()
        groups_dns = obj.object.get("groups", [])
        res = defaultdict(list)
        for group_dn in groups_dns:
            group_match = self.class_dn_regex.match(group_dn)
            if group_match:
                if group_match["ou"] in known_schools:
                    res[group_match["ou"]].append(group_match["name"])
                else:
                    self.logger.warning(
                        "Ignoring unknown OU %r in 'school_classes' of %r (%r).",
                        group_match["ou"],
                        obj,
                        group_dn,
                    )
        res = dict(res.items())
        self.logger.debug("User %r has school_classes: %r.", obj.username, res)
        return res

    @staticmethod
    async def _handle_attr_source_uid(obj: ListenerUserAddModifyObject) -> str:
        """Get a source_uid."""
        return obj.source_uid or await get_source_uid()

    def _handle_none_value(self, key_here: str) -> Any:
        """
        A target API may have problems with `none` values. Here the value can
        either be changed (return something else) or a `SkipAttribute`
        exception can be raised to not map (send) the attribute at all.
        """
        return None

    def _update_for_mapping_data(self, key_here: str, key_there: str, value_here: Any) -> Dict[str, Any]:
        """
        Structure the data mapping result for the target API.

        For example:

            if key_there in MAIN_ATTRIBUTES:
                return {key_there: value_here}
            else:
                return {"udm_properties": {key_there: value_here}}

        :param key_here: attribute name at sender
        :param key_there: attribute name at receiver
        :param value_here: data to send
        :return: dict that will be used to `update()` the complete data
            mapping dict of `map_attributes`
        """
        return {key_there: value_here}

    def _handle_password_hashes(self, obj: ListenerUserAddModifyObject) -> Dict[str, Any]:
        """
        If password hashed should be sent, return them here.

        :return: dict to update the mapping data
        """
        return {}

    async def shutdown(self) -> None:
        """Clean shutdown procedure."""
        pass


class UserHandlerPluginBase(abc.ABC):
    """
    Base class for plugins handling user objects.

    Send current state of user to target system (school authority).

    Each out queue has its own `UserHandlerPerSchoolAuthorityBase` instance
    which handles user data for its queues school authority.
    """

    plugin_name = ""
    user_handler_class: Type[PerSchoolAuthorityUserHandlerBase] = None  # set this to your class
    _user_handlers: Dict[Tuple[str, str], PerSchoolAuthorityUserHandlerBase] = dict()

    def __init__(self):
        self.logger = ConsoleAndFileLogging.get_logger(self.__class__.__name__)

    @hook_impl
    async def shutdown(self) -> None:
        """impl for ucsschool_id_connector.plugins.Preprocessing.shutdown"""
        for user_handler in self._user_handlers.values():
            await user_handler.shutdown()

    @hook_impl
    async def create_request_kwargs(
        self, http_method: str, url: str, school_authority: SchoolAuthorityConfiguration
    ) -> Dict[Any, Any]:
        """impl for ucsschool_id_connector.plugins.Postprocessing.create_request_kwargs"""
        return {}

    @hook_impl
    async def handle_listener_object(
        self, school_authority: SchoolAuthorityConfiguration, obj: ListenerObject
    ) -> bool:
        """impl for ucsschool_id_connector.plugins.Postprocessing.handle_listener_object"""
        if isinstance(obj, ListenerUserAddModifyObject):
            await self.user_handler(school_authority, self.plugin_name).handle_create_or_update(obj)
        elif isinstance(obj, ListenerUserRemoveObject):
            await self.user_handler(school_authority, self.plugin_name).handle_remove(obj)
        else:
            return False
        return True

    @hook_impl
    async def school_authority_ping(self, school_authority: SchoolAuthorityConfiguration) -> bool:
        """impl for ucsschool_id_connector.plugins.Postprocessing.school_authority_ping"""
        user_handler = self.user_handler(school_authority, self.plugin_name)
        try:
            await user_handler.refresh_roles()
            await user_handler.refresh_schools()
        except APICommunicationError as exc:
            self.logger.error(
                "Error calling school authority API (%s): %s",
                school_authority.name,
                exc,
            )
            return False
        return True

    @classmethod
    def user_handler(
        cls, school_authority: SchoolAuthorityConfiguration, plugin_name: str
    ) -> PerSchoolAuthorityUserHandlerBase:
        key = (school_authority.name, plugin_name)
        if key not in cls._user_handlers:
            cls._user_handlers[key] = cls.user_handler_class(school_authority, plugin_name)
        return cls._user_handlers[key]
