"""
Camera Topology Learning
Automatically learns spatial relationships between cameras based on person tracking data
"""

from db_connection import get_connection as db_get_connection, get_cursor
from typing import Dict, List, Tuple, Optional
from collections import defaultdict
import json
from datetime import datetime

class CameraTopologyLearner:
    def __init__(self, db_path: str = None):
        # db_path parameter kept for backwards compatibility but ignored
        # Now using shared PostgreSQL connection pool
        pass

    def get_connection(self):
        return db_get_connection()

    def analyze_person_transitions(self, person_name: str) -> List[Dict]:
        """
        Analyze all detections of a person across videos to find transitions
        Returns list of transitions with timing and camera info
        """
        conn = self.get_connection()
        cursor = conn.cursor()

        # Get all detections for this person, ordered by time
        cursor.execute('''
            SELECT
                ka.id,
                ka.video_id,
                ka.timestamp,
                ka.created_date,
                v.title as video_title,
                v.notes as video_notes
            FROM keyframe_annotations ka
            JOIN videos v ON ka.video_id = v.id
            JOIN annotation_tags at ON ka.id = at.annotation_id
            WHERE at.annotation_type = 'keyframe'
            AND at.tag_value LIKE %s
            ORDER BY ka.created_date, ka.timestamp
        ''', (f'%{person_name}%',))

        detections = [dict(row) for row in cursor.fetchall()]

        # Group by video (camera)
        transitions = []
        for i in range(len(detections) - 1):
            current = detections[i]
            next_det = detections[i + 1]

            # If different videos, it's a potential transition
            if current['video_id'] != next_det['video_id']:
                # Calculate time delta
                curr_time = datetime.fromisoformat(current['created_date'])
                next_time = datetime.fromisoformat(next_det['created_date'])
                time_delta = (next_time - curr_time).total_seconds()

                # Extract camera identifiers from video titles/notes
                from_camera = self._extract_camera_id(current['video_title'], current['video_notes'])
                to_camera = self._extract_camera_id(next_det['video_title'], next_det['video_notes'])

                transitions.append({
                    'person_name': person_name,
                    'from_video_id': current['video_id'],
                    'from_camera': from_camera,
                    'from_timestamp': current['timestamp'],
                    'to_video_id': next_det['video_id'],
                    'to_camera': to_camera,
                    'to_timestamp': next_det['timestamp'],
                    'time_delta_seconds': time_delta,
                    'from_detection_id': current['id'],
                    'to_detection_id': next_det['id']
                })

        return transitions

    def _extract_camera_id(self, video_title: str, video_notes: Optional[str]) -> str:
        """
        Extract camera identifier from video metadata
        Looks for patterns like: "Camera 1", "Cam A", "camera_entrance", etc.
        """
        import re

        # Try video notes first (more likely to have structured data)
        if video_notes:
            # Look for camera_id in notes
            match = re.search(r'camera[_\s]*(%s:id)%s[_\s]*[:]%s\s*([a-zA-Z0-9_-]+)', video_notes.lower())
            if match:
                return match.group(1)

        # Try video title
        if video_title:
            # Look for common camera naming patterns
            patterns = [
                r'camera[_\s]*([a-zA-Z0-9_-]+)',
                r'cam[_\s]*([a-zA-Z0-9_-]+)',
                r'view[_\s]*([a-zA-Z0-9_-]+)',
            ]
            for pattern in patterns:
                match = re.search(pattern, video_title.lower())
                if match:
                    return match.group(1)

        # Fallback: use video title itself
        return video_title or 'unknown'

    def build_camera_graph(self) -> Dict:
        """
        Build a graph of camera relationships based on all person transitions
        Returns: {
            'nodes': [{'id': camera_id, 'label': camera_name, 'detection_count': N}],
            'edges': [{'from': cam1, 'to': cam2, 'transitions': N, 'avg_time': seconds}]
        }
        """
        conn = self.get_connection()
        cursor = conn.cursor()

        # Get all people with names
        cursor.execute('''
            SELECT DISTINCT
                REPLACE(REPLACE(tag_value, '"person_name":', ''), '"', '') as person_name
            FROM annotation_tags
            WHERE annotation_type = 'keyframe'
            AND tag_value LIKE '%person_name%'
            AND tag_value NOT LIKE '%Unknown%'
        ''')

        people = [row['person_name'] for row in cursor.fetchall() if row['person_name']]

        # Analyze transitions for each person
        all_transitions = []
        for person in people:
            transitions = self.analyze_person_transitions(person)
            all_transitions.extend(transitions)

        # Build graph
        nodes = defaultdict(lambda: {'detection_count': 0, 'people_seen': set()})
        edges = defaultdict(lambda: {'transitions': [], 'people': set()})

        for trans in all_transitions:
            from_cam = trans['from_camera']
            to_cam = trans['to_camera']
            person = trans['person_name']

            # Update nodes
            nodes[from_cam]['detection_count'] += 1
            nodes[from_cam]['people_seen'].add(person)
            nodes[to_cam]['detection_count'] += 1
            nodes[to_cam]['people_seen'].add(person)

            # Update edges
            edge_key = (from_cam, to_cam)
            edges[edge_key]['transitions'].append(trans['time_delta_seconds'])
            edges[edge_key]['people'].add(person)

        # Format output
        graph_nodes = [
            {
                'id': cam_id,
                'label': cam_id,
                'detection_count': data['detection_count'],
                'people_count': len(data['people_seen'])
            }
            for cam_id, data in nodes.items()
        ]

        graph_edges = []
        for (from_cam, to_cam), data in edges.items():
            times = data['transitions']
            graph_edges.append({
                'from': from_cam,
                'to': to_cam,
                'transition_count': len(times),
                'avg_time_seconds': sum(times) / len(times) if times else 0,
                'min_time_seconds': min(times) if times else 0,
                'max_time_seconds': max(times) if times else 0,
                'people_count': len(data['people'])
            })

        return {
            'nodes': graph_nodes,
            'edges': graph_edges,
            'total_transitions': len(all_transitions),
            'unique_people': len(people)
        }

    def suggest_track_links(self, person_name: str, time_window_seconds: float = 300) -> List[Dict]:
        """
        Suggest which unassigned detections might be the same person
        based on learned camera topology and timing patterns

        Args:
            person_name: Name of person to find additional detections for
            time_window_seconds: Max time between detections to consider (default 5 min)

        Returns:
            List of suggested matches with confidence scores
        """
        conn = self.get_connection()
        cursor = conn.cursor()

        # Get known detections for this person
        cursor.execute('''
            SELECT
                ka.id,
                ka.video_id,
                ka.timestamp,
                ka.created_date,
                v.title as video_title,
                v.notes as video_notes
            FROM keyframe_annotations ka
            JOIN videos v ON ka.video_id = v.id
            JOIN annotation_tags at ON ka.id = at.annotation_id
            WHERE at.annotation_type = 'keyframe'
            AND at.tag_value LIKE %s
            ORDER BY ka.created_date
        ''', (f'%{person_name}%',))

        known_detections = [dict(row) for row in cursor.fetchall()]

        # Get all unassigned detections (no person_name or "Unknown")
        cursor.execute('''
            SELECT DISTINCT
                ka.id,
                ka.video_id,
                ka.timestamp,
                ka.created_date,
                v.title as video_title,
                v.notes as video_notes
            FROM keyframe_annotations ka
            JOIN videos v ON ka.video_id = v.id
            WHERE NOT EXISTS (
                SELECT 1 FROM annotation_tags at
                WHERE at.annotation_id = ka.id
                AND at.annotation_type = 'keyframe'
                AND at.tag_value LIKE '%person_name%'
                AND at.tag_value NOT LIKE '%Unknown%'
            )
            ORDER BY ka.created_date
        ''')

        unassigned = [dict(row) for row in cursor.fetchall()]

        # Learn typical transition patterns
        graph = self.build_camera_graph()
        edge_map = {}
        for edge in graph['edges']:
            key = (edge['from'], edge['to'])
            edge_map[key] = edge

        # Find suggestions
        suggestions = []

        for known in known_detections:
            known_camera = self._extract_camera_id(known['video_title'], known['video_notes'])
            known_time = datetime.fromisoformat(known['created_date'])

            for unknown in unassigned:
                unknown_camera = self._extract_camera_id(unknown['video_title'], unknown['video_notes'])
                unknown_time = datetime.fromisoformat(unknown['created_date'])

                # Skip if same camera
                if known['video_id'] == unknown['video_id']:
                    continue

                # Calculate time delta
                time_delta = abs((unknown_time - known_time).total_seconds())

                # Skip if outside time window
                if time_delta > time_window_seconds:
                    continue

                # Check if this camera transition is known
                edge_key = (known_camera, unknown_camera)
                reverse_edge_key = (unknown_camera, known_camera)

                confidence = 0.0
                reasoning = []

                # Factor 1: Known camera transition
                if edge_key in edge_map:
                    edge = edge_map[edge_key]
                    expected_time = edge['avg_time_seconds']
                    time_diff = abs(time_delta - expected_time)

                    # Confidence based on how close to expected time
                    if time_diff < 30:  # Within 30 seconds
                        confidence += 0.5
                        reasoning.append(f"Typical transition time (~{expected_time:.0f}s)")
                    elif time_diff < 60:  # Within 1 minute
                        confidence += 0.3
                        reasoning.append(f"Reasonable transition time (~{expected_time:.0f}s)")
                    else:
                        confidence += 0.1
                        reasoning.append(f"Possible transition (~{expected_time:.0f}s typical)")

                elif reverse_edge_key in edge_map:
                    confidence += 0.2
                    reasoning.append("Reverse of known transition")

                # Factor 2: Time proximity (closer = higher confidence)
                if time_delta < 30:
                    confidence += 0.3
                    reasoning.append("Very close in time (<30s)")
                elif time_delta < 60:
                    confidence += 0.2
                    reasoning.append("Close in time (<1min)")
                elif time_delta < 180:
                    confidence += 0.1
                    reasoning.append("Within 3 minutes")

                # Factor 3: Sequential order
                if unknown_time > known_time:
                    confidence += 0.1
                    reasoning.append("Chronologically after")

                # Only suggest if confidence > 0.2
                if confidence > 0.2:
                    suggestions.append({
                        'unknown_detection_id': unknown['id'],
                        'unknown_video_id': unknown['video_id'],
                        'unknown_camera': unknown_camera,
                        'unknown_timestamp': unknown['timestamp'],
                        'related_detection_id': known['id'],
                        'related_camera': known_camera,
                        'time_delta_seconds': time_delta,
                        'confidence': min(confidence, 1.0),  # Cap at 1.0
                        'reasoning': reasoning
                    })

        # Sort by confidence
        suggestions.sort(key=lambda x: x['confidence'], reverse=True)

        return suggestions

    def get_person_movement_path(self, person_name: str) -> List[Dict]:
        """
        Get the complete movement path of a person across all cameras
        Returns chronological list of detections with camera transitions
        """
        transitions = self.analyze_person_transitions(person_name)

        if not transitions:
            return []

        # Build path
        path = []
        for i, trans in enumerate(transitions):
            if i == 0:
                # Add starting point
                path.append({
                    'sequence': 0,
                    'camera': trans['from_camera'],
                    'video_id': trans['from_video_id'],
                    'timestamp': trans['from_timestamp'],
                    'detection_id': trans['from_detection_id'],
                    'event': 'first_detection'
                })

            # Add transition
            path.append({
                'sequence': i + 1,
                'camera': trans['to_camera'],
                'video_id': trans['to_video_id'],
                'timestamp': trans['to_timestamp'],
                'detection_id': trans['to_detection_id'],
                'event': 'transition',
                'from_camera': trans['from_camera'],
                'transition_time_seconds': trans['time_delta_seconds']
            })

        return path
