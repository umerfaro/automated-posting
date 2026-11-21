"""Operator commands. Run inside the deployment with its database and environment."""
import argparse

from app.config import Settings
from app.services.store import Store


def main():
    parser=argparse.ArgumentParser(description='Dispatch operator management')
    parser.add_argument('command',choices=['promote'])
    parser.add_argument('email',nargs='?')
    args=parser.parse_args()
    settings=Settings.from_env()
    email=(args.email or settings.admin_email).strip().lower()
    if not email:
        parser.error('Supply the registered email, or configure ADMIN_EMAIL.')
    store=Store(settings.database_path)
    with store.connect() as db:
        exists=db.execute('SELECT id FROM users WHERE email=?',(email,)).fetchone()
        if not exists:
            parser.error('Register this account in Dispatch first.')
        db.execute('UPDATE users SET admin=1 WHERE id=?',(exists[0],))
    print('Administrator access granted. Refresh the signed-in account.')


if __name__=='__main__':
    main()
