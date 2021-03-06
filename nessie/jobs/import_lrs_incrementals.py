"""
Copyright ©2020. The Regents of the University of California (Regents). All Rights Reserved.

Permission to use, copy, modify, and distribute this software and its documentation
for educational, research, and not-for-profit purposes, without fee and without a
signed licensing agreement, is hereby granted, provided that the above copyright
notice, this paragraph and the following two paragraphs appear in all copies,
modifications, and distributions.

Contact The Office of Technology Licensing, UC Berkeley, 2150 Shattuck Avenue,
Suite 510, Berkeley, CA 94720-1620, (510) 643-7201, otl@berkeley.edu,
http://ipira.berkeley.edu/industry-info for commercial licensing opportunities.

IN NO EVENT SHALL REGENTS BE LIABLE TO ANY PARTY FOR DIRECT, INDIRECT, SPECIAL,
INCIDENTAL, OR CONSEQUENTIAL DAMAGES, INCLUDING LOST PROFITS, ARISING OUT OF
THE USE OF THIS SOFTWARE AND ITS DOCUMENTATION, EVEN IF REGENTS HAS BEEN ADVISED
OF THE POSSIBILITY OF SUCH DAMAGE.

REGENTS SPECIFICALLY DISCLAIMS ANY WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE. THE
SOFTWARE AND ACCOMPANYING DOCUMENTATION, IF ANY, PROVIDED HEREUNDER IS PROVIDED
"AS IS". REGENTS HAS NO OBLIGATION TO PROVIDE MAINTENANCE, SUPPORT, UPDATES,
ENHANCEMENTS, OR MODIFICATIONS.
"""

from datetime import datetime
from os import path
from time import sleep

from flask import current_app as app
from nessie.externals import dms, lrs, redshift, s3
from nessie.jobs.background_job import BackgroundJob, BackgroundJobError
from nessie.lib.util import localize_datetime, resolve_sql_template

"""Logic for LRS incremental import job."""


class ImportLrsIncrementals(BackgroundJob):

    def run(self, truncate_lrs=True):
        app.logger.info('Starting DMS replication task...')
        task_id = app.config['LRS_CANVAS_INCREMENTAL_REPLICATION_TASK_ID']

        self.transient_bucket = app.config['LRS_CANVAS_INCREMENTAL_TRANSIENT_BUCKET']
        self.transient_path = app.config['LRS_CANVAS_INCREMENTAL_TRANSIENT_PATH']

        self.delete_old_incrementals()

        response = dms.start_replication_task(task_id)
        if not response:
            raise BackgroundJobError('Failed to start DMS replication task (response={response}).')

        while True:
            response = dms.get_replication_task(task_id)
            if response.get('Status') == 'stopped':
                if response.get('StopReason') == 'Stop Reason FULL_LOAD_ONLY_FINISHED':
                    app.logger.info('DMS replication task completed')
                    break
                else:
                    raise BackgroundJobError(f'Replication task stopped for unexpected reason: {response}')
            sleep(10)

        lrs_response = lrs.fetch('select count(*) from statements')
        if lrs_response:
            self.lrs_statement_count = lrs_response[0][0]
        else:
            raise BackgroundJobError(f'Failed to retrieve LRS statements for comparison.')

        transient_keys = s3.get_keys_with_prefix(self.transient_path, bucket=self.transient_bucket)
        if not transient_keys:
            raise BackgroundJobError('Could not retrieve S3 keys from transient bucket.')
        self.verify_and_unload_transient()

        timestamp_path = localize_datetime(datetime.now()).strftime('%Y/%m/%d/%H%M%S')
        destination_path = app.config['LRS_CANVAS_INCREMENTAL_DESTINATION_PATH'] + '/' + timestamp_path
        for destination_bucket in app.config['LRS_CANVAS_INCREMENTAL_DESTINATION_BUCKETS']:
            self.migrate_transient_to_destination(
                transient_keys,
                destination_bucket,
                destination_path,
            )

        if truncate_lrs:
            if lrs.execute('TRUNCATE statements'):
                app.logger.info('Truncated incremental LRS table.')
            else:
                raise BackgroundJobError('Failed to truncate incremental LRS table.')

        return (
            f'Migrated {self.lrs_statement_count} statements to S3'
            f"(buckets={app.config['LRS_CANVAS_INCREMENTAL_DESTINATION_BUCKETS']}, path={destination_path})"
        )

    def delete_old_incrementals(self):
        old_incrementals = s3.get_keys_with_prefix(self.transient_path, bucket=self.transient_bucket)
        if old_incrementals is None:
            raise BackgroundJobError('Error listing old incrementals, aborting job.')
        if len(old_incrementals) > 0:
            delete_response = s3.delete_objects(old_incrementals, bucket=self.transient_bucket)
            if not delete_response:
                raise BackgroundJobError(f'Error deleting old incremental files from {self.transient_bucket}, aborting job.')
            else:
                app.logger.info(f'Deleted {len(old_incrementals)} old incremental files from {self.transient_bucket}.')

    def delete_old_unloads(self):
        old_unloads = s3.get_keys_with_prefix(app.config['LRS_CANVAS_INCREMENTAL_ETL_PATH_REDSHIFT'], bucket=self.transient_bucket)
        if old_unloads is None:
            raise BackgroundJobError('Error listing old unloads, aborting job.')
        if len(old_unloads) > 0:
            delete_response = s3.delete_objects(old_unloads, bucket=self.transient_bucket)
            if not delete_response:
                raise BackgroundJobError(f'Error deleting old unloads from {self.transient_bucket}, aborting job.')
            else:
                app.logger.info(f'Deleted {len(old_unloads)} old unloads from {self.transient_bucket}.')

    def migrate_transient_to_destination(self, keys, destination_bucket, destination_path):
        destination_url = 's3://' + destination_bucket + '/' + destination_path
        redshift_schema = app.config['REDSHIFT_SCHEMA_LRS']

        for transient_key in keys:
            destination_key = transient_key.replace(self.transient_path, destination_path)
            if not s3.copy(self.transient_bucket, transient_key, destination_bucket, destination_key):
                raise BackgroundJobError(f'Copy from transient bucket to destination bucket {destination_bucket} failed.')
        self.verify_migration(destination_url, redshift_schema)
        redshift.drop_external_schema(redshift_schema)

    def unload_to_etl(self, schema, bucket, timestamped=True):
        s3_url = 's3://' + bucket + '/' + app.config['LRS_CANVAS_INCREMENTAL_ETL_PATH_REDSHIFT']
        if timestamped:
            s3_url += '/' + localize_datetime(datetime.now()).strftime('%Y/%m/%d/statements_%Y%m%d_%H%M%S_')
        else:
            s3_url += '/statements'

        redshift_iam_role = app.config['REDSHIFT_IAM_ROLE']
        if not redshift.execute(
            f"""
                UNLOAD ('SELECT statement FROM {schema}.statements')
                TO '{s3_url}'
                IAM_ROLE '{redshift_iam_role}'
                ENCRYPTED
                DELIMITER AS '  '
                NULL AS ''
                ALLOWOVERWRITE
                PARALLEL OFF
                MAXFILESIZE 1 gb
            """,
        ):
            raise BackgroundJobError(f'Error executing Redshift unload to {s3_url}.')
        self.verify_unloaded_count(s3_url)

    def verify_and_unload_transient(self):
        transient_url = f's3://{self.transient_bucket}/{self.transient_path}'
        transient_schema = app.config['REDSHIFT_SCHEMA_LRS'] + '_transient'
        self.verify_migration(transient_url, transient_schema)
        self.delete_old_unloads()
        self.unload_to_etl(transient_schema, self.transient_bucket, timestamped=False)
        redshift.drop_external_schema(transient_schema)

    def verify_migration(self, incremental_url, incremental_schema):
        redshift.drop_external_schema(incremental_schema)
        resolved_ddl_transient = resolve_sql_template(
            'create_lrs_statements_table.template.sql',
            redshift_schema_lrs_external=incremental_schema,
            loch_s3_lrs_statements_path=incremental_url,
        )
        if redshift.execute_ddl_script(resolved_ddl_transient):
            app.logger.info(f"LRS incremental schema '{incremental_schema}' created.")
        else:
            raise BackgroundJobError(f"LRS incremental schema '{incremental_schema}' creation failed.")

        redshift_response = redshift.fetch(f'select count(*) from {incremental_schema}.statements')
        if redshift_response:
            redshift_statement_count = redshift_response[0].get('count')
        else:
            raise BackgroundJobError(f"Failed to verify LRS incremental schema '{incremental_schema}'.")

        if redshift_statement_count == self.lrs_statement_count:
            app.logger.info(f'Verified {redshift_statement_count} rows migrated from LRS to {incremental_url}.')
        else:
            raise BackgroundJobError(
                f'Discrepancy between LRS ({self.lrs_statement_count} statements)'
                f' and {incremental_url} ({redshift_statement_count} statements).')

    def verify_unloaded_count(self, url):
        url = path.split(url)[0]
        schema = app.config['REDSHIFT_SCHEMA_LRS']
        resolved_ddl_transient_unloaded = resolve_sql_template(
            'create_lrs_statements_unloaded_table.template.sql',
            redshift_schema_lrs_external=schema,
            loch_s3_lrs_statements_unloaded_path=url,
        )
        if redshift.execute_ddl_script(resolved_ddl_transient_unloaded):
            app.logger.info(f"statements_unloaded table created in schema '{schema}'.")
        else:
            raise BackgroundJobError(f"Failed to create statements_unloaded table in schema '{schema}'.")

        redshift_response = redshift.fetch(f'select count(*) from {schema}.statements_unloaded')
        if redshift_response:
            unloaded_statement_count = redshift_response[0].get('count')
        else:
            raise BackgroundJobError('Failed to get unloaded statement count.')

        if unloaded_statement_count == self.lrs_statement_count:
            app.logger.info(f'Verified {unloaded_statement_count} unloaded from LRS to {url}.')
        else:
            raise BackgroundJobError(
                f'Discrepancy between LRS ({self.lrs_statement_count} statements)'
                f' and {url} ({unloaded_statement_count} statements).')
