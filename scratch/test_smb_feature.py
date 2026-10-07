import os
import sys
import unittest

# Ensure root directory is on python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import create_app
from models import db, ServerConnection, Module, User, AuditLog
from services.smb_service import parse_smb_auth, normalize_unc_path, format_file_size, join_unc_path


class TestSMBFeature(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()

    def test_parse_smb_auth(self):
        domain, user = parse_smb_auth(r"domain123\user456")
        self.assertEqual(domain, "domain123")
        self.assertEqual(user, "user456")

        domain, user = parse_smb_auth("user456@corp.pirtek.com")
        self.assertEqual(domain, "corp.pirtek.com")
        self.assertEqual(user, "user456")

        domain, user = parse_smb_auth("simpleuser")
        self.assertEqual(domain, "")
        self.assertEqual(user, "simpleuser")

    def test_normalize_unc_path(self):
        p1 = normalize_unc_path("10.1.2.3", "SharedFolder/Uploads")
        self.assertEqual(p1, r"\\10.1.2.3\SharedFolder\Uploads")

        p2 = normalize_unc_path("10.1.2.3", r"\\10.1.2.3\SharedFolder\Uploads")
        self.assertEqual(p2, r"\\10.1.2.3\SharedFolder\Uploads")

        p3 = normalize_unc_path("10.1.2.3", r"\\FILESERVER1\Share\Data")
        self.assertEqual(p3, r"\\FILESERVER1\Share\Data")

    def test_join_unc_path(self):
        j1 = join_unc_path(r"\\10.101.1.5\KML", "file.kml")
        self.assertEqual(j1, r"\\10.101.1.5\KML\file.kml")

        j2 = join_unc_path(r"\\10.101.1.5\KML\\", "file.kml")
        self.assertEqual(j2, r"\\10.101.1.5\KML\file.kml")

        j3 = join_unc_path(r"\\10.101.1.5\KML/sub/", "/file.kml")
        self.assertEqual(j3, r"\\10.101.1.5\KML\sub\file.kml")

    def test_format_file_size(self):
        self.assertEqual(format_file_size(500), "500 B")
        self.assertEqual(format_file_size(1024), "1.0 KB")
        self.assertEqual(format_file_size(1048576), "1.0 MB")
        self.assertEqual(format_file_size(1073741824), "1.0 GB")

    def test_module_model_fields(self):
        with self.app.app_context():
            conn1 = ServerConnection.query.filter_by(name="TEST_SMB_CONN_1").first()
            if not conn1:
                conn1 = ServerConnection(
                    name="TEST_SMB_CONN_1",
                    server_type="windows_share",
                    host="10.1.2.3",
                    username=r"domain123\user456",
                    password="Password1234"
                )
                db.session.add(conn1)
                db.session.commit()

            # Create test upload module
            upload_mod = Module(
                name="Test Upload Module",
                description="Test file uploader",
                object_type="file_upload",
                connection_id=conn1.id,
                destination_filepath=r"\\10.1.2.3\SharedFolder\Uploads"
            )
            db.session.add(upload_mod)
            db.session.commit()

            # Create test mover module
            mover_mod = Module(
                name="Test Mover Module",
                description="Test file mover",
                object_type="file_mover",
                connection_id=conn1.id,
                destination_connection_id=conn1.id,
                origin_filepath=r"\\10.1.2.3\SharedFolder\Incoming",
                destination_filepath=r"\\10.1.2.3\SharedFolder\Archive",
                file_pattern="*.csv;*.xlsx"
            )
            db.session.add(mover_mod)
            db.session.commit()

            # Verify saved attributes
            fetched_upload = Module.query.get(upload_mod.id)
            self.assertEqual(fetched_upload.object_type, "file_upload")
            self.assertEqual(fetched_upload.destination_filepath, r"\\10.1.2.3\SharedFolder\Uploads")

            fetched_mover = Module.query.get(mover_mod.id)
            self.assertEqual(fetched_mover.object_type, "file_mover")
            self.assertEqual(fetched_mover.file_pattern, "*.csv;*.xlsx")
            self.assertEqual(fetched_mover.destination_connection_id, conn1.id)

            # Cleanup
            db.session.delete(upload_mod)
            db.session.delete(mover_mod)
            db.session.delete(conn1)
            db.session.commit()

    def test_routes_rendering(self):
        with self.app.app_context():
            # Clean any old test records if present
            old_mods = Module.query.filter(Module.name.in_(["Test Upload UI", "Test Mover UI"])).all()
            for m in old_mods:
                db.session.delete(m)
            old_conn = ServerConnection.query.filter_by(name="TEST_SMB_RENDER").first()
            if old_conn:
                db.session.delete(old_conn)
            db.session.commit()

            # Create a test admin user if none exists
            admin_user = User.query.filter_by(email="testadmin@example.com").first()
            if not admin_user:
                admin_user = User(email="testadmin@example.com", role="admin")
                admin_user.set_password("AdminPass123!")
                db.session.add(admin_user)
                db.session.commit()

            # Create test SMB connection and modules
            conn = ServerConnection(
                name="TEST_SMB_RENDER",
                server_type="windows_share",
                host="10.1.2.3",
                username=r"domain\user",
                password="pass"
            )
            db.session.add(conn)
            db.session.commit()

            upload_mod = Module(
                name="Test Upload UI",
                object_type="file_upload",
                connection_id=conn.id,
                destination_filepath=r"\\10.1.2.3\Share\Uploads"
            )
            mover_mod = Module(
                name="Test Mover UI",
                object_type="file_mover",
                connection_id=conn.id,
                destination_connection_id=conn.id,
                origin_filepath=r"\\10.1.2.3\Share\Incoming",
                destination_filepath=r"\\10.1.2.3\Share\Archive"
            )
            db.session.add_all([upload_mod, mover_mod])
            admin_user.modules.extend([upload_mod, mover_mod])
            db.session.commit()

            # Login as admin
            with self.client.session_transaction() as sess:
                sess['_user_id'] = str(admin_user.id)
                sess['_fresh'] = True

            # Test admin pages
            res_conn = self.client.get('/admin/connections')
            self.assertEqual(res_conn.status_code, 200)
            self.assertIn(b'Windows File Server (SMB / Share)', res_conn.data)

            res_mod = self.client.get('/admin/modules')
            self.assertEqual(res_mod.status_code, 200)
            self.assertIn(b'File Upload (Windows Server / SMB)', res_mod.data)
            self.assertIn(b'File Mover / Transfer (Windows Server / SMB)', res_mod.data)

            # Test portal execute pages for the new module types
            res_up = self.client.get(f'/portal/execute/{upload_mod.id}')
            self.assertEqual(res_up.status_code, 200)
            self.assertIn(b'Drag & Drop Files Here', res_up.data)

            res_mv = self.client.get(f'/portal/execute/{mover_mod.id}')
            self.assertEqual(res_mv.status_code, 200)
            self.assertIn(b'Move Selected', res_mv.data)

            # Test execution API error responses (always returns JSON)
            # 1. Upload without files -> 400 JSON
            res_up_empty = self.client.post(f'/portal/execute/file_upload/{upload_mod.id}', data={})
            self.assertEqual(res_up_empty.status_code, 400)
            data_up_empty = res_up_empty.get_json()
            self.assertFalse(data_up_empty['success'])
            self.assertIn('error', data_up_empty)

            # 2. Mover without filenames -> 400 JSON
            res_mv_empty = self.client.post(f'/portal/execute/file_mover/{mover_mod.id}', json={})
            self.assertEqual(res_mv_empty.status_code, 400)
            data_mv_empty = res_mv_empty.get_json()
            self.assertFalse(data_mv_empty['success'])
            self.assertIn('error', data_mv_empty)

            # 3. Nonexistent module -> 404 JSON
            res_404 = self.client.get('/portal/api/modules/99999/files')
            self.assertEqual(res_404.status_code, 404)
            data_404 = res_404.get_json()
            self.assertFalse(data_404['success'])

            # Test custom connection test API (invalid host will fail cleanly)
            res_test = self.client.post('/admin/api/connections/test-custom', json={
                'server_type': 'windows_share',
                'host': '127.0.0.1',
                'username': r'test\user',
                'password': 'invalidpassword'
            })
            self.assertEqual(res_test.status_code, 400)
            data_test = res_test.get_json()
            self.assertIn('success', data_test)
            self.assertFalse(data_test['success'])

            # Test browse folders API (nonexistent / unreachable host returns error JSON)
            res_browse = self.client.get(f'/admin/api/connections/{conn.id}/browse-folders?path=')
            self.assertEqual(res_browse.status_code, 400)
            data_browse = res_browse.get_json()
            self.assertIn('error', data_browse)
            self.assertFalse(data_browse['success'])

            # Cleanup
            db.session.delete(upload_mod)
            db.session.delete(mover_mod)
            db.session.delete(conn)
            db.session.delete(admin_user)
            db.session.commit()


if __name__ == '__main__':
    unittest.main()


