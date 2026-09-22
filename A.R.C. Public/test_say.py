import unittest
from types import SimpleNamespace
from unittest.mock import patch

from main import member_has_global_staff_role, repository


class SayAuthorizationTests(unittest.TestCase):
    def test_requires_the_configured_global_staff_role(self):
        staff_member = SimpleNamespace(roles=[SimpleNamespace(id=100)])
        head_staff_only = SimpleNamespace(roles=[SimpleNamespace(id=200)])
        admin_only = SimpleNamespace(
            roles=[],
            guild_permissions=SimpleNamespace(administrator=True),
        )

        with patch.object(
            repository,
            "get_server_config",
            return_value=SimpleNamespace(
                staff_role_id=100,
                head_staff_role_id=200,
            ),
        ):
            self.assertTrue(member_has_global_staff_role(staff_member, 1))
            self.assertFalse(member_has_global_staff_role(head_staff_only, 1))
            self.assertFalse(member_has_global_staff_role(admin_only, 1))


if __name__ == "__main__":
    unittest.main()