import unittest
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.auth import create_access_token, get_current_user


class TenantAuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://')
        self.db = Session(self.engine)
        self.db.execute(text("ATTACH DATABASE ':memory:' AS public"))
        self.db.execute(text('CREATE TABLE public.tenants (id INTEGER PRIMARY KEY, schema_name TEXT)'))
        self.db.execute(text('CREATE TABLE public.users (id INTEGER PRIMARY KEY, tenant_id INTEGER, username TEXT, is_admin BOOLEAN)'))
        self.db.execute(text("INSERT INTO public.tenants VALUES (2, 'tenant_current')"))
        self.db.execute(text("INSERT INTO public.users VALUES (10, 2, 'tester', true)"))

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_stale_token_tenant_is_replaced_by_database_association(self):
        token = create_access_token({'sub': 'tester', 'tenant_id': 1, 'schema_name': 'tenant_old'})
        user = get_current_user(token, self.db)
        self.assertEqual(user['tenant_id'], 2)
        self.assertEqual(user['schema_name'], 'tenant_current')

    def test_token_only_needs_user_identity(self):
        token = create_access_token({'sub': 'tester'})
        self.assertEqual(get_current_user(token, self.db)['schema_name'], 'tenant_current')

    def test_missing_user_or_tenant_is_rejected(self):
        token = create_access_token({'sub': 'tester'})
        for table in ('tenants', 'users'):
            with self.subTest(table=table):
                with self.db.begin_nested() as transaction:
                    self.db.execute(text(f'DELETE FROM public.{table}'))
                    with self.assertRaises(HTTPException) as error:
                        get_current_user(token, self.db)
                    self.assertEqual(error.exception.status_code, 401)
                    transaction.rollback()

    def test_expired_token_is_rejected(self):
        token = create_access_token({'sub': 'tester'}, timedelta(seconds=-1))
        with self.assertRaises(HTTPException) as error:
            get_current_user(token, self.db)
        self.assertEqual(error.exception.status_code, 401)
