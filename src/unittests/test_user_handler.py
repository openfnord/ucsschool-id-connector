# -*- coding: utf-8 -*-

# Copyright 2019 Univention GmbH
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

import datetime
from unittest.mock import patch

import pytest
from faker import Faker

import id_sync.models
import id_sync.user_handler

fake = Faker()


@pytest.mark.asyncio
async def test_map_attributes(
    listener_user_add_modify_object, school_authority_configuration
):
    # TODO: use SchoolAuthorityConfiguration from integration_tests fixture
    s_a_config = school_authority_configuration()
    user_handler = id_sync.user_handler.UserHandler(s_a_config)
    user_obj: id_sync.models.ListenerUserAddModifyObject = listener_user_add_modify_object()
    user_handler._api_schools_cache = dict(
        (ou, fake.uri()) for ou in user_obj.object["school"]
    )
    user_handler._api_schools_cache_creation = datetime.datetime.now()
    user_handler.api_roles_cache = dict(
        (role.name, fake.uri()) for role in user_obj.school_user_roles
    )

    async def get_source_uid():
        return "TESTID"

    with patch.object(id_sync.user_handler, "get_source_uid", get_source_uid):
        res = await user_handler.map_attributes(user_obj)
    school = [ou for ou in user_obj.object["school"] if ou in user_obj.dn][0]
    school_uri = (await user_handler.api_schools_cache)[school]
    assert res == {
        "disabled": user_obj.object["disabled"],
        "firstname": user_obj.object["firstname"],
        "lastname": user_obj.object["lastname"],
        "name": user_obj.object["username"],
        "record_uid": user_obj.object["record_uid"],
        "roles": list(user_handler.api_roles_cache.values()),
        "school": school_uri,
        "school_classes": user_obj.object.get("school_classes", {}),
        "schools": list((await user_handler.api_schools_cache).values()),
        "source_uid": user_obj.object["source_uid"],
        "udm_properties": {
            "id_sync_pw": {
                "krb5Key": [k.decode() for k in user_obj.user_passwords.krb5Key],
                "krb5KeyVersionNumber": user_obj.user_passwords.krb5KeyVersionNumber,
                "sambaNTPassword": user_obj.user_passwords.sambaNTPassword,
                "sambaPwdLastSet": user_obj.user_passwords.sambaPwdLastSet,
                "userPassword": user_obj.user_passwords.userPassword,
            }
        },
    }
