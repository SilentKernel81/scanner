# ============================================================================
# FILE: server/support_api.py
# The support file from the Settings page (core/support.py makes it): what it would hold, and the file itself.
# Both behind the settings password when one is set. That lock is optional, so it is not what keeps secrets out: the file is cleaned when it is made.
# ============================================================================
from flask import jsonify, send_file
import tempfile

from card_scanner.core import support
from card_scanner.server.logs import logger
from card_scanner.server.lock import guarded
from card_scanner.server.state import app


@app.route('/api/support/preview')
@guarded
def support_preview():
    """The files a support file would hold, with the size of each log, before it is made"""
    return jsonify({'success': True, 'files': support.preview()})


@app.route('/api/support/bundle')
@guarded
def support_bundle():
    """The support file: made now, handed over, and not kept on the server"""
    packed = tempfile.TemporaryFile()
    try:
        support.write_bundle(packed)
    except support.SupportError as e:
        packed.close()                     # never handed out: it holds what could not be removed
        logger.error(f"Support file: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    except Exception as e:
        packed.close()
        logger.exception(f"Support file: could not be made: {e}")
        return jsonify({'success': False, 'error': f'The support file could not be made: {e}'}), 500
    logger.info("Support file downloaded")
    return send_file(packed, mimetype='application/zip', as_attachment=True, download_name=support.bundle_name(), max_age=0)
