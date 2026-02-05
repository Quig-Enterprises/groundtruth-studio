#!/bin/bash
# scripts/setup_postgres.sh
# Run as: sudo -u postgres bash scripts/setup_postgres.sh

set -e

DB_NAME="groundtruth_studio"
DB_USER="groundtruth"
DB_PASS="$(openssl rand -base64 32)"

echo "Creating PostgreSQL database and user..."

psql <<EOF
CREATE USER $DB_USER WITH PASSWORD '$DB_PASS';
CREATE DATABASE $DB_NAME OWNER $DB_USER;
GRANT ALL PRIVILEGES ON DATABASE $DB_NAME TO $DB_USER;
\c $DB_NAME
GRANT ALL ON SCHEMA public TO $DB_USER;
EOF

echo ""
echo "=== PostgreSQL Setup Complete ==="
echo "Database: $DB_NAME"
echo "User: $DB_USER"
echo "Password: $DB_PASS"
echo ""
echo "Add to environment:"
echo "export DATABASE_URL=\"postgresql://$DB_USER:$DB_PASS@localhost:5432/$DB_NAME\""
