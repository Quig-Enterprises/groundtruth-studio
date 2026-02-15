"""
GPU Manager - Manages GPU allocation between inference and training.

Strategy:
- GPU 0: Frigate (always) + CompreFace (always) + training (shared)
- GPU 1: Argus + InsightFace + tracker + training (shared)
- When training starts: pause inference services, free VRAM
- When training ends: resume inference services
"""

import json
import logging
import os
import signal
import subprocess
import time
from datetime import datetime, timezone
from db_connection import get_cursor

logger = logging.getLogger('gpu_manager')

# Service configurations
INFERENCE_SERVICES = {
    'argus-detector': {
        'gpu': 1,
        'systemd_unit': 'argus-detector.service',
        'vram_mb': 4000,
        'priority': 2,  # lower = resume first
    },
    'insightface-api': {
        'gpu': 1,
        'systemd_unit': 'insightface-api.service',
        'vram_mb': 2000,
        'priority': 3,
    },
    'tracker-service': {
        'gpu': -1,  # CPU only
        'systemd_unit': 'tracker-service.service',
        'vram_mb': 0,
        'priority': 1,
    },
}


class GPUManager:
    def __init__(self):
        self.state_file = '/opt/groundtruth-studio/data/gpu_manager_state.json'
        self.paused_services = set()
        self._load_state()

    def _load_state(self):
        """Load persisted state from disk."""
        try:
            if os.path.exists(self.state_file):
                with open(self.state_file) as f:
                    state = json.load(f)
                    self.paused_services = set(state.get('paused_services', []))
        except Exception as e:
            logger.warning(f"Could not load state: {e}")

    def _save_state(self):
        """Persist state to disk."""
        try:
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
            with open(self.state_file, 'w') as f:
                json.dump({
                    'paused_services': list(self.paused_services),
                    'updated_at': datetime.now(timezone.utc).isoformat()
                }, f, indent=2)
        except Exception as e:
            logger.error(f"Could not save state: {e}")

    def get_gpu_memory(self, gpu_id: int) -> dict:
        """Get GPU memory usage via nvidia-smi."""
        try:
            result = subprocess.run(
                ['nvidia-smi', '--query-gpu=memory.used,memory.total,memory.free',
                 '--format=csv,noheader,nounits', f'--id={gpu_id}'],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                used, total, free = [int(x.strip()) for x in result.stdout.strip().split(',')]
                return {'gpu_id': gpu_id, 'used_mb': used, 'total_mb': total, 'free_mb': free}
        except Exception as e:
            logger.error(f"nvidia-smi error: {e}")
        return {'gpu_id': gpu_id, 'used_mb': -1, 'total_mb': -1, 'free_mb': -1}

    def get_all_gpu_status(self) -> list:
        """Get status of all GPUs."""
        return [self.get_gpu_memory(i) for i in range(2)]

    def pause_inference(self, gpu_id: int = None, reason: str = 'training'):
        """Pause inference services to free GPU memory for training.

        Args:
            gpu_id: Specific GPU to free (None = all)
            reason: Reason for pausing (for logging)
        """
        logger.info(f"Pausing inference services (gpu={gpu_id}, reason={reason})")

        for name, cfg in INFERENCE_SERVICES.items():
            if gpu_id is not None and cfg['gpu'] != gpu_id:
                continue
            if cfg['gpu'] == -1:  # CPU-only service, skip
                continue
            if name in self.paused_services:
                continue

            try:
                subprocess.run(
                    ['sudo', 'systemctl', 'stop', cfg['systemd_unit']],
                    capture_output=True, timeout=30
                )
                self.paused_services.add(name)
                logger.info(f"Stopped {name} ({cfg['systemd_unit']})")
            except Exception as e:
                logger.error(f"Failed to stop {name}: {e}")

        self._save_state()

    def resume_inference(self, gpu_id: int = None):
        """Resume paused inference services after training completes.

        Services are resumed in priority order (lower number = first).
        """
        logger.info(f"Resuming inference services (gpu={gpu_id})")

        # Sort by priority
        services_to_resume = sorted(
            [(name, cfg) for name, cfg in INFERENCE_SERVICES.items()
             if name in self.paused_services
             and (gpu_id is None or cfg['gpu'] == gpu_id)],
            key=lambda x: x[1]['priority']
        )

        for name, cfg in services_to_resume:
            try:
                subprocess.run(
                    ['sudo', 'systemctl', 'start', cfg['systemd_unit']],
                    capture_output=True, timeout=30
                )
                self.paused_services.discard(name)
                logger.info(f"Started {name} ({cfg['systemd_unit']})")
                # Wait for service to load model before starting next
                time.sleep(5)
            except Exception as e:
                logger.error(f"Failed to start {name}: {e}")

        self._save_state()

    def get_service_status(self) -> list:
        """Get status of all managed services."""
        statuses = []
        for name, cfg in INFERENCE_SERVICES.items():
            try:
                result = subprocess.run(
                    ['systemctl', 'is-active', cfg['systemd_unit']],
                    capture_output=True, text=True, timeout=5
                )
                status = result.stdout.strip()
            except Exception:
                status = 'unknown'

            statuses.append({
                'name': name,
                'unit': cfg['systemd_unit'],
                'gpu': cfg['gpu'],
                'vram_mb': cfg['vram_mb'],
                'status': status,
                'paused': name in self.paused_services
            })
        return statuses

    def check_training_active(self) -> bool:
        """Check if any training job is currently running."""
        try:
            with get_cursor(commit=False) as cur:
                cur.execute("""
                    SELECT COUNT(*) FROM training_jobs
                    WHERE status = 'running'
                """)
                count = cur.fetchone()[0]
                return count > 0
        except Exception:
            return False


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    mgr = GPUManager()

    print("\n=== GPU Status ===")
    for gpu in mgr.get_all_gpu_status():
        print(f"  GPU {gpu['gpu_id']}: {gpu['used_mb']}MB / {gpu['total_mb']}MB (free: {gpu['free_mb']}MB)")

    print("\n=== Service Status ===")
    for svc in mgr.get_service_status():
        paused_str = " [PAUSED]" if svc['paused'] else ""
        print(f"  {svc['name']}: {svc['status']}{paused_str} (GPU {svc['gpu']}, {svc['vram_mb']}MB)")

    print(f"\nTraining active: {mgr.check_training_active()}")
