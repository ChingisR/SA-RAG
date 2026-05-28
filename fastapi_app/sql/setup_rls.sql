-- Enable Row-Level Security on the hr_employees table
ALTER TABLE hr_employees ENABLE ROW LEVEL SECURITY;
-- Force RLS even for the table owner/superuser so our shared connection pool isolates correctly
ALTER TABLE hr_employees FORCE ROW LEVEL SECURITY;

-- Drop existing policies just to be idempotent
DROP POLICY IF EXISTS "hr_admin_all" ON hr_employees;

-- Create policy allowing HR_Admin full access based on the transaction session variable
CREATE POLICY "hr_admin_all" ON hr_employees
FOR SELECT
TO public
USING (current_setting('app.current_user_role', true) = 'HR_Admin');

-- If you wanted a standard user policy (e.g. they can only see their OWN salary), you would add it here:
-- CREATE POLICY "standard_user_self" ON hr_employees
-- FOR SELECT TO public
-- USING (current_setting('app.current_user_email', true) = email);
