import unittest
from unittest.mock import MagicMock, patch
import core.tasks as tasks


class IdentityMatchingTests(unittest.TestCase):
    def test_empty_remark_still_matches_nickname_row_by_configured_id(self):
        response = MagicMock()
        response.url = 'https://www.douyin.com/aweme/v1/web/im/user/info/'
        response.json.return_value = {'data': [{'nickname': 'Nickname', 'remark_name': '', 'unique_id': 'friend_id', 'short_id': '123', 'sec_uid': 'private_identity'}]}
        with patch.object(tasks, 'userIDDict', {}):
            tasks.handle_response(response)
            self.assertEqual(tasks.checkTargetName('Nickname', ['friend_id']), 'friend_id')
