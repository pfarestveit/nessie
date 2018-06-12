"""
Copyright ©2018. The Regents of the University of California (Regents). All Rights Reserved.

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


"""Cleanup and resync job to handle failures from sync_canvas_snapshots."""


import time

from flask import current_app as app
from nessie.externals import s3
from nessie.jobs.background_job import BackgroundJob, get_s3_canvas_daily_path
from nessie.lib import metadata
from nessie.lib.dispatcher import dispatch


def generate_job_id():
    return 'resync_' + str(int(time.time()))


class ResyncCanvasSnapshots(BackgroundJob):

    def run(self, cleanup=True):
        job_id = generate_job_id()
        app.logger.info(f'Starting Canvas snapshot resync job... (id={job_id})')
        md = metadata.get_failures_from_last_sync()
        if not md['failures']:
            app.logger.info(f"No failures found for job_id {md['job_id']}, skipping resync.")
            return
        app.logger.info(f"Found {len(md['failures'])} failures for job_id {md['job_id']}, attempting resync.")

        failures = 0
        successes = 0

        for failure in md['failures']:
            if cleanup and failure.destination_url:
                destination_key = failure.destination_url.split(app.config['LOCH_S3_BUCKET'] + '/')[1]
                if s3.delete_objects([destination_key]):
                    metadata.delete_canvas_snapshots([destination_key])
                else:
                    app.logger.error(f'Could not delete failed snapshot from S3 (url={failure.destination_url})')
            metadata.create_canvas_sync_status(
                job_id=job_id,
                filename=failure.filename,
                canvas_table=failure.canvas_table,
                # The original signed source URL will remain valid if the resync job is run within an hour of the sync job.
                # TODO Add logic to fetch a new signed URL from the Canvas Data API for older jobs.
                source_url=failure.source_url,
            )

            # Regenerate the S3 key, since the failed job may not have progressed far enough to store a destination URL in its metadata.
            if failure.canvas_table == 'requests':
                key_components = [app.config['LOCH_S3_CANVAS_DATA_PATH_CURRENT_TERM'], failure.canvas_table, failure.filename]
            else:
                key_components = [get_s3_canvas_daily_path(), failure.canvas_table, failure.filename]
            key = '/'.join(key_components)
            response = dispatch('sync_file_to_s3', data={'canvas_sync_job_id': job_id, 'url': failure.source_url, 'key': key})

            if not response:
                app.logger.error('Failed to dispatch S3 resync of snapshot ' + failure.filename)
                metadata.update_canvas_sync_status(job_id, key, 'error', details=f'Failed to dispatch: {response}')
                failures += 1
            else:
                app.logger.info('Dispatched S3 resync of snapshot ' + failure.filename)
                successes += 1

        app.logger.info(f'Canvas snapshot resync job dispatched to workers ({successes} successful dispatches, {failures} failures).')