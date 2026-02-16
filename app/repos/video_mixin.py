import json
from typing import List, Dict, Optional

import psycopg2
from psycopg2 import extras

from db_connection import get_cursor


class VideoMixin:
    """Video CRUD, tags, libraries, and statistics."""

    def add_video(self, filename: str, original_url: str = None, title: str = None,
                  duration: float = None, width: int = None, height: int = None,
                  file_size: int = None, thumbnail_path: str = None, notes: str = None,
                  camera_id: str = None, metadata: dict = None) -> int:
        import json as _json
        with get_cursor() as cursor:
            cursor.execute('''
                INSERT INTO videos (filename, original_url, title, duration, width, height,
                                  file_size, thumbnail_path, notes, camera_id, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (filename) DO NOTHING
                RETURNING id
            ''', (filename, original_url, title, duration, width, height, file_size, thumbnail_path, notes, camera_id,
                  _json.dumps(metadata) if metadata else '{}'))
            result = cursor.fetchone()
            if result:
                return result['id']
            # Row already existed — look it up
            cursor.execute('SELECT id FROM videos WHERE filename = %s', (filename,))
            existing = cursor.fetchone()
            return existing['id']

    def get_video(self, video_id: int) -> Optional[Dict]:
        with get_cursor(commit=False) as cursor:
            cursor.execute('SELECT * FROM videos WHERE id = %s', (video_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def _is_default_library(self, library_id: int) -> bool:
        """Check if a library is the default (Uncategorized) library."""
        with get_cursor(commit=False) as cursor:
            cursor.execute('SELECT is_default FROM content_libraries WHERE id = %s', (library_id,))
            row = cursor.fetchone()
            return bool(row and row['is_default'])

    def get_all_videos(self, limit: int = 100, offset: int = 0, library_id: int = None) -> List[Dict]:
        with get_cursor(commit=False) as cursor:
            if library_id is not None and self._is_default_library(library_id):
                # Default library: show videos NOT in any non-default library
                cursor.execute('''
                    SELECT v.*,
                           STRING_AGG(DISTINCT t.name, ', ') as tags,
                           COUNT(DISTINCT ka.id) as annotation_count
                    FROM videos v
                    LEFT JOIN video_tags vt ON v.id = vt.video_id
                    LEFT JOIN tags t ON vt.tag_id = t.id
                    LEFT JOIN keyframe_annotations ka ON v.id = ka.video_id
                    WHERE NOT EXISTS (
                        SELECT 1 FROM content_library_items cli2
                        INNER JOIN content_libraries cl2 ON cli2.library_id = cl2.id
                        WHERE cli2.video_id = v.id AND cl2.is_default = FALSE
                    )
                    GROUP BY v.id
                    ORDER BY v.upload_date DESC
                    LIMIT %s OFFSET %s
                ''', (limit, offset))
            elif library_id is not None:
                cursor.execute('''
                    SELECT v.*,
                           STRING_AGG(DISTINCT t.name, ', ') as tags,
                           COUNT(DISTINCT ka.id) as annotation_count
                    FROM videos v
                    INNER JOIN content_library_items cli ON v.id = cli.video_id AND cli.library_id = %s
                    LEFT JOIN video_tags vt ON v.id = vt.video_id
                    LEFT JOIN tags t ON vt.tag_id = t.id
                    LEFT JOIN keyframe_annotations ka ON v.id = ka.video_id
                    GROUP BY v.id
                    ORDER BY v.upload_date DESC
                    LIMIT %s OFFSET %s
                ''', (library_id, limit, offset))
            else:
                cursor.execute('''
                    SELECT v.*,
                           STRING_AGG(DISTINCT t.name, ', ') as tags,
                           COUNT(DISTINCT ka.id) as annotation_count
                    FROM videos v
                    LEFT JOIN video_tags vt ON v.id = vt.video_id
                    LEFT JOIN tags t ON vt.tag_id = t.id
                    LEFT JOIN keyframe_annotations ka ON v.id = ka.video_id
                    GROUP BY v.id
                    ORDER BY v.upload_date DESC
                    LIMIT %s OFFSET %s
                ''', (limit, offset))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def search_videos(self, query: str) -> List[Dict]:
        with get_cursor(commit=False) as cursor:
            search_term = f'%{query}%'
            cursor.execute('''
                SELECT v.*,
                       (SELECT STRING_AGG(DISTINCT t2.name, ', ')
                        FROM video_tags vt2
                        JOIN tags t2 ON vt2.tag_id = t2.id
                        WHERE vt2.video_id = v.id) as tags,
                       (SELECT COUNT(*)
                        FROM keyframe_annotations ka2
                        WHERE ka2.video_id = v.id) as annotation_count
                FROM videos v
                LEFT JOIN video_tags vt ON v.id = vt.video_id
                LEFT JOIN tags t ON vt.tag_id = t.id
                LEFT JOIN keyframe_annotations ka ON v.id = ka.video_id
                LEFT JOIN annotation_tags at ON ka.id = at.annotation_id AND at.annotation_type = 'keyframe'
                LEFT JOIN tag_groups tg ON at.group_id = tg.id
                WHERE v.title LIKE %s OR v.filename LIKE %s OR v.notes LIKE %s OR t.name LIKE %s
                   OR ka.activity_tag LIKE %s OR ka.comment LIKE %s
                   OR tg.group_name LIKE %s OR at.tag_value LIKE %s
                GROUP BY v.id
                ORDER BY v.upload_date DESC
            ''', (search_term, search_term, search_term, search_term,
                  search_term, search_term, search_term, search_term))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def add_tag(self, name: str, category: str = None) -> int:
        with get_cursor() as cursor:
            try:
                cursor.execute('''
                    INSERT INTO tags (name, category) VALUES (%s, %s)
                    RETURNING id
                ''', (name, category))
                result = cursor.fetchone()
                return result['id']
            except psycopg2.IntegrityError:
                # Tag already exists, fetch its id
                cursor.connection.rollback()
                cursor.execute('SELECT id FROM tags WHERE name = %s', (name,))
                result = cursor.fetchone()
                return result['id']

    def tag_video(self, video_id: int, tag_name: str) -> bool:
        tag_id = self.add_tag(tag_name)
        with get_cursor() as cursor:
            try:
                cursor.execute('INSERT INTO video_tags (video_id, tag_id) VALUES (%s, %s)',
                             (video_id, tag_id))
                return True
            except psycopg2.IntegrityError:
                return False

    def untag_video(self, video_id: int, tag_name: str) -> bool:
        with get_cursor() as cursor:
            cursor.execute('''
                DELETE FROM video_tags
                WHERE video_id = %s AND tag_id = (SELECT id FROM tags WHERE name = %s)
            ''', (video_id, tag_name))
            return cursor.rowcount > 0

    def get_all_tags(self) -> List[Dict]:
        with get_cursor(commit=False) as cursor:
            cursor.execute('''
                SELECT t.*, COUNT(vt.video_id) as video_count
                FROM tags t
                LEFT JOIN video_tags vt ON t.id = vt.tag_id
                GROUP BY t.id
                ORDER BY t.name
            ''')
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def get_all_time_range_tag_names(self) -> List[str]:
        with get_cursor(commit=False) as cursor:
            cursor.execute('SELECT DISTINCT tag_name FROM time_range_tags ORDER BY tag_name')
            rows = cursor.fetchall()
            return [row['tag_name'] for row in rows]

    def get_video_behaviors(self, video_id: int) -> List[Dict]:
        with get_cursor(commit=False) as cursor:
            cursor.execute('SELECT * FROM behaviors WHERE video_id = %s ORDER BY start_time', (video_id,))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def delete_video(self, video_id: int) -> bool:
        with get_cursor() as cursor:
            cursor.execute('DELETE FROM videos WHERE id = %s', (video_id,))
            return cursor.rowcount > 0

    def get_bboxes_for_video_ids(self, video_ids: list) -> dict:
        """Get bboxes from keyframe annotations and AI predictions for multiple videos"""
        if not video_ids:
            return {}
        with get_cursor(commit=False) as cursor:
            placeholders = ','.join(['%s'] * len(video_ids))
            # Get keyframe annotations
            cursor.execute(f'''
                SELECT video_id, bbox_x, bbox_y, bbox_width, bbox_height, reviewed
                FROM keyframe_annotations
                WHERE video_id IN ({placeholders})
                AND bbox_x IS NOT NULL AND bbox_width > 0
            ''', video_ids)
            rows = cursor.fetchall()
            result = {}
            for row in rows:
                vid = row['video_id']
                if vid not in result:
                    result[vid] = []
                result[vid].append({
                    'x': row['bbox_x'],
                    'y': row['bbox_y'],
                    'w': row['bbox_width'],
                    'h': row['bbox_height'],
                    'reviewed': bool(row['reviewed'])
                })
            # Get AI predictions (always shown as unvalidated)
            cursor.execute(f'''
                SELECT video_id, bbox_x, bbox_y, bbox_width, bbox_height
                FROM ai_predictions
                WHERE video_id IN ({placeholders})
                AND bbox_x IS NOT NULL AND bbox_width > 0
                AND review_status IN ('pending', 'needs_correction')
            ''', video_ids)
            rows = cursor.fetchall()
            for row in rows:
                vid = row['video_id']
                if vid not in result:
                    result[vid] = []
                result[vid].append({
                    'x': row['bbox_x'],
                    'y': row['bbox_y'],
                    'w': row['bbox_width'],
                    'h': row['bbox_height'],
                    'reviewed': False
                })
            return result

    def update_video(self, video_id: int, **kwargs) -> bool:
        """Update video fields"""
        allowed_fields = ['filename', 'original_url', 'title', 'duration', 'width', 'height',
                         'file_size', 'thumbnail_path', 'notes', 'camera_id']

        updates = []
        values = []
        for field, value in kwargs.items():
            if field in allowed_fields:
                updates.append(f'{field} = %s')
                values.append(value)

        if not updates:
            return False

        values.append(video_id)
        with get_cursor() as cursor:
            cursor.execute(f'''
                UPDATE videos SET {', '.join(updates)}
                WHERE id = %s
            ''', values)
            return cursor.rowcount > 0

    def get_video_by_filename(self, filename: str) -> Optional[Dict]:
        """Get video by filename"""
        with get_cursor(commit=False) as cursor:
            cursor.execute('SELECT * FROM videos WHERE filename = %s', (filename,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_video_tags(self, video_id: int) -> List[Dict]:
        """Get all tags for a specific video"""
        with get_cursor(commit=False) as cursor:
            cursor.execute('''
                SELECT t.*
                FROM tags t
                JOIN video_tags vt ON t.id = vt.tag_id
                WHERE vt.video_id = %s
                ORDER BY t.name
            ''', (video_id,))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def get_videos_by_tag(self, tag_name: str) -> List[Dict]:
        """Get all videos with a specific tag"""
        with get_cursor(commit=False) as cursor:
            cursor.execute('''
                SELECT v.*
                FROM videos v
                JOIN video_tags vt ON v.id = vt.video_id
                JOIN tags t ON vt.tag_id = t.id
                WHERE t.name = %s
                ORDER BY v.upload_date DESC
            ''', (tag_name,))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def get_total_video_count(self) -> int:
        """Get total number of videos"""
        with get_cursor(commit=False) as cursor:
            cursor.execute('SELECT COUNT(*) as count FROM videos')
            result = cursor.fetchone()
            return result['count'] if result else 0

    def get_total_annotation_count(self) -> int:
        """Get total number of keyframe annotations"""
        with get_cursor(commit=False) as cursor:
            cursor.execute('SELECT COUNT(*) as count FROM keyframe_annotations')
            result = cursor.fetchone()
            return result['count'] if result else 0

    def get_statistics(self) -> Dict:
        """Get database statistics"""
        with get_cursor(commit=False) as cursor:
            stats = {}

            cursor.execute('SELECT COUNT(*) as count FROM videos')
            stats['total_videos'] = cursor.fetchone()['count']

            cursor.execute('SELECT COUNT(*) as count FROM keyframe_annotations')
            stats['total_annotations'] = cursor.fetchone()['count']

            cursor.execute('SELECT COUNT(*) as count FROM keyframe_annotations WHERE reviewed = true')
            stats['reviewed_annotations'] = cursor.fetchone()['count']

            cursor.execute('SELECT COUNT(*) as count FROM tags')
            stats['total_tags'] = cursor.fetchone()['count']

            cursor.execute('SELECT COUNT(*) as count FROM fleet_vehicles')
            stats['total_fleet_vehicles'] = cursor.fetchone()['count']

            cursor.execute('SELECT COUNT(*) as count FROM trailers')
            stats['total_trailers'] = cursor.fetchone()['count']

            cursor.execute('SELECT COUNT(*) as count FROM yolo_export_configs')
            stats['total_export_configs'] = cursor.fetchone()['count']

            return stats

    # ==================== Content Libraries ====================

    def get_all_libraries(self) -> List[Dict]:
        """Get all content libraries with item counts.
        The default (Uncategorized) library count = videos not in any other library."""
        with get_cursor(commit=False) as cursor:
            cursor.execute('''
                SELECT cl.*,
                       COUNT(cli.video_id) as item_count
                FROM content_libraries cl
                LEFT JOIN content_library_items cli ON cl.id = cli.library_id
                WHERE cl.is_default = FALSE
                GROUP BY cl.id
                ORDER BY cl.name ASC
            ''')
            non_default = [dict(row) for row in cursor.fetchall()]

            # Count videos not in any non-default library for Uncategorized
            cursor.execute('''
                SELECT cl.*, (
                    SELECT COUNT(*) FROM videos v
                    WHERE NOT EXISTS (
                        SELECT 1 FROM content_library_items cli2
                        INNER JOIN content_libraries cl2 ON cli2.library_id = cl2.id
                        WHERE cli2.video_id = v.id AND cl2.is_default = FALSE
                    )
                ) as item_count
                FROM content_libraries cl
                WHERE cl.is_default = TRUE
            ''')
            default_row = cursor.fetchone()
            result = []
            if default_row:
                result.append(dict(default_row))
            result.extend(non_default)
            return result

    def create_library(self, name: str) -> int:
        """Create a new content library. Returns its id."""
        with get_cursor() as cursor:
            cursor.execute('''
                INSERT INTO content_libraries (name)
                VALUES (%s)
                RETURNING id
            ''', (name,))
            return cursor.fetchone()['id']

    def rename_library(self, library_id: int, name: str) -> bool:
        """Rename a library. Cannot rename the default."""
        with get_cursor() as cursor:
            cursor.execute('''
                UPDATE content_libraries
                SET name = %s
                WHERE id = %s AND is_default = FALSE
            ''', (name, library_id))
            return cursor.rowcount > 0

    def delete_library(self, library_id: int) -> bool:
        """Delete a library. Cannot delete the default. Items become unassigned."""
        with get_cursor() as cursor:
            cursor.execute('''
                DELETE FROM content_libraries
                WHERE id = %s AND is_default = FALSE
            ''', (library_id,))
            return cursor.rowcount > 0

    def add_to_library(self, library_id: int, video_ids: List[int]) -> int:
        """Add videos to a library. Returns count of newly added."""
        added = 0
        with get_cursor() as cursor:
            for vid in video_ids:
                try:
                    cursor.execute('''
                        INSERT INTO content_library_items (library_id, video_id)
                        VALUES (%s, %s)
                        ON CONFLICT DO NOTHING
                    ''', (library_id, vid))
                    added += cursor.rowcount
                except Exception:
                    pass
        return added

    def remove_from_library(self, library_id: int, video_id: int) -> bool:
        """Remove a video from a library."""
        with get_cursor() as cursor:
            cursor.execute('''
                DELETE FROM content_library_items
                WHERE library_id = %s AND video_id = %s
            ''', (library_id, video_id))
            return cursor.rowcount > 0

    def get_video_libraries(self, video_id: int) -> List[Dict]:
        """Get all libraries a video belongs to."""
        with get_cursor(commit=False) as cursor:
            cursor.execute('''
                SELECT cl.id, cl.name, cl.is_default
                FROM content_libraries cl
                INNER JOIN content_library_items cli ON cl.id = cli.library_id
                WHERE cli.video_id = %s
                ORDER BY cl.name
            ''', (video_id,))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def get_next_unannotated_in_library(self, library_id: int, current_video_id: int = None) -> Optional[Dict]:
        """Get the next video in a library that has zero keyframe annotations.
        For the default library, finds videos not in any non-default library.
        Skips the current video if provided. Returns None if all are annotated."""
        with get_cursor(commit=False) as cursor:
            if self._is_default_library(library_id):
                # Default library: videos not in any non-default library
                cursor.execute('''
                    SELECT v.id, v.filename, v.title
                    FROM videos v
                    LEFT JOIN keyframe_annotations ka ON v.id = ka.video_id
                    WHERE v.id != COALESCE(%s, -1)
                      AND v.filename NOT LIKE '%%.placeholder'
                      AND NOT EXISTS (
                          SELECT 1 FROM content_library_items cli2
                          INNER JOIN content_libraries cl2 ON cli2.library_id = cl2.id
                          WHERE cli2.video_id = v.id AND cl2.is_default = FALSE
                      )
                    GROUP BY v.id
                    HAVING COUNT(ka.id) = 0
                    ORDER BY v.upload_date ASC
                    LIMIT 1
                ''', (current_video_id,))
            else:
                cursor.execute('''
                    SELECT v.id, v.filename, v.title
                    FROM videos v
                    INNER JOIN content_library_items cli ON v.id = cli.video_id AND cli.library_id = %s
                    LEFT JOIN keyframe_annotations ka ON v.id = ka.video_id
                    WHERE v.id != COALESCE(%s, -1)
                    GROUP BY v.id
                    HAVING COUNT(ka.id) = 0
                    ORDER BY v.upload_date ASC
                    LIMIT 1
                ''', (library_id, current_video_id))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_next_unannotated(self, current_video_id: int = None) -> Optional[Dict]:
        """Get the next video (globally) that has zero keyframe annotations.
        Skips the current video if provided. Returns None if all are annotated."""
        with get_cursor(commit=False) as cursor:
            cursor.execute('''
                SELECT v.id, v.filename, v.title
                FROM videos v
                LEFT JOIN keyframe_annotations ka ON v.id = ka.video_id
                WHERE v.id != COALESCE(%s, -1)
                  AND v.filename NOT LIKE '%%.placeholder'
                GROUP BY v.id
                HAVING COUNT(ka.id) = 0
                ORDER BY v.upload_date ASC
                LIMIT 1
            ''', (current_video_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
