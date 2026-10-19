"""The optional legacy LAN listener follows the engine's pause/quit lifetime."""
import argparse
from pathlib import Path
from panelyra.services.worker_runtime import interruptible_worker,WorkerInterrupted
from panelyra.services.companion_runtime import record


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--port',type=int,default=8588);parser.add_argument('--bind',default='0.0.0.0');parser.add_argument('--no-advertise',action='store_true');args=parser.parse_args()
    try:
        with interruptible_worker(args.root,'companion'):
            import tablet_companion as companion
            # The service runs the installed root, not a user browser profile.
            companion.APP_DIR=args.root.resolve();companion.STATE_DIR=companion.APP_DIR/'data/tablet_companion'
            for name,file in [('CERT_FILE','companion.crt'),('KEY_FILE','companion.key'),('PAIR_CODE_FILE','pair-code.json'),
                              ('CERT_HOST_FILE','certificate-host.txt'),('SYNC_REQUEST_FILE','sync-request.json'),('SYNC_STATUS_FILE','tablet-sync-status.json')]:
                setattr(companion,name,companion.STATE_DIR/file)
            record(args.root,status='running',error='')
            command=['--port',str(args.port),'--bind',args.bind,'--no-usb-watch']
            if args.no_advertise:command.append('--no-advertise')
            return companion.main(command)
    except WorkerInterrupted:
        from panelyra.engine import policy
        record(args.root,status='queued' if policy(args.root) else 'stopped',error='Paused or stopped with the Panelyra engine.')
        return 130
    except Exception as error:
        record(args.root,status='failed',error=str(error)[:250]);return 1


if __name__=='__main__':raise SystemExit(main())
