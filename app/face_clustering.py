"""
HDBSCAN-based Face Clustering Module

Periodically clusters unassigned face embeddings to create "Unknown #N" identities
for groups of similar faces that appear frequently across the dataset.
"""

import logging
import numpy as np
from typing import List, Tuple, Dict, Optional
from datetime import datetime
import hdbscan
from db_connection import get_cursor, get_connection
from psycopg2 import extras

logger = logging.getLogger(__name__)


class FaceClusterer:
    """Clusters face embeddings using HDBSCAN to identify recurring unknown faces."""

    def __init__(self, min_cluster_size=5, min_samples=3, metric='euclidean'):
        """
        Initialize face clustering with HDBSCAN parameters.

        Args:
            min_cluster_size: Minimum faces to form a cluster (default 5)
            min_samples: HDBSCAN min_samples parameter for noise reduction (default 3)
            metric: Distance metric - 'euclidean' works well for normalized embeddings
        """
        self.min_cluster_size = min_cluster_size
        self.min_samples = min_samples
        self.metric = metric
        logger.info(
            f"FaceClusterer initialized: min_cluster_size={min_cluster_size}, "
            f"min_samples={min_samples}, metric={metric}"
        )

    def get_unassigned_embeddings(self) -> Tuple[List[str], np.ndarray]:
        """
        Fetch face embeddings not linked to any named identity.

        Returns:
            Tuple of (embedding_ids, vectors_matrix)
            - embedding_ids: List of embedding UUIDs
            - vectors_matrix: NumPy array of shape (n_embeddings, embedding_dim)
        """
        query = """
            SELECT e.embedding_id, e.vector
            FROM embeddings e
            LEFT JOIN identities i ON e.identity_id = i.identity_id
            WHERE e.embedding_type = 'face'
            AND (i.name IS NULL OR i.name LIKE 'Unknown #%')
            ORDER BY e.created_at
        """

        with get_cursor(commit=False) as cursor:
            cursor.execute(query)
            rows = cursor.fetchall()

        if not rows:
            logger.info("No unassigned face embeddings found")
            return [], np.array([])

        embedding_ids = [row['embedding_id'] for row in rows]
        vectors = np.array([row['vector'] for row in rows])

        logger.info(f"Retrieved {len(embedding_ids)} unassigned face embeddings")
        return embedding_ids, vectors

    def cluster(self, vectors: np.ndarray) -> np.ndarray:
        """
        Run HDBSCAN clustering on face embedding vectors.

        Args:
            vectors: NumPy array of shape (n_samples, n_features)

        Returns:
            Cluster labels array (same length as vectors)
            - Non-negative integers for cluster assignments
            - -1 for noise points
        """
        if len(vectors) < self.min_cluster_size:
            logger.warning(
                f"Too few embeddings ({len(vectors)}) for clustering "
                f"(min_cluster_size={self.min_cluster_size})"
            )
            return np.full(len(vectors), -1)

        clusterer = hdbscan.HDBSCAN(
            min_cluster_size=self.min_cluster_size,
            min_samples=self.min_samples,
            metric=self.metric,
            cluster_selection_method='eom'  # Excess of Mass
        )

        labels = clusterer.fit_predict(vectors)

        n_clusters = len(set(labels)) - (1 if -1 in labels else 0)
        n_noise = list(labels).count(-1)

        logger.info(
            f"HDBSCAN clustering complete: {n_clusters} clusters found, "
            f"{n_noise} noise points"
        )

        return labels

    def get_next_unknown_number(self) -> int:
        """
        Get the next available 'Unknown #N' number.

        Returns:
            Integer for next unknown identity (e.g., 1 if none exist, or max+1)
        """
        query = """
            SELECT name FROM identities
            WHERE name LIKE 'Unknown #%'
            ORDER BY name DESC
            LIMIT 1
        """

        with get_cursor(commit=False) as cursor:
            cursor.execute(query)
            row = cursor.fetchone()

        if not row:
            return 1

        # Extract number from "Unknown #123"
        try:
            last_num = int(row['name'].split('#')[1])
            return last_num + 1
        except (IndexError, ValueError):
            logger.warning(f"Could not parse unknown number from: {row['name']}")
            return 1

    def create_cluster_identities(
        self, embedding_ids: List[str], labels: np.ndarray
    ) -> Dict[int, str]:
        """
        Create or update identities for each cluster and link embeddings.

        For each cluster (non-negative label):
        1. Check if embeddings already have a cluster identity
        2. If not, create new "Unknown #N" identity
        3. Link all embeddings in cluster to this identity

        Args:
            embedding_ids: List of embedding UUIDs
            labels: Cluster labels from HDBSCAN (same length as embedding_ids)

        Returns:
            Dictionary mapping {cluster_label: identity_id}
        """
        cluster_map = {}
        unique_clusters = set(label for label in labels if label >= 0)

        if not unique_clusters:
            logger.info("No valid clusters to create identities for")
            return cluster_map

        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=extras.RealDictCursor)

            for cluster_id in sorted(unique_clusters):
                # Get embeddings in this cluster
                cluster_mask = labels == cluster_id
                cluster_embedding_ids = [
                    embedding_ids[i] for i, is_in_cluster in enumerate(cluster_mask)
                    if is_in_cluster
                ]

                # Check if any embedding already has an identity
                check_query = """
                    SELECT DISTINCT i.identity_id, i.name
                    FROM embeddings e
                    JOIN identities i ON e.identity_id = i.identity_id
                    WHERE e.embedding_id = ANY(%s)
                    AND i.name LIKE 'Unknown #%'
                    LIMIT 1
                """
                cursor.execute(check_query, (cluster_embedding_ids,))
                existing = cursor.fetchone()

                if existing:
                    identity_id = existing['identity_id']
                    logger.info(
                        f"Cluster {cluster_id} already has identity: "
                        f"{existing['name']} ({identity_id})"
                    )
                else:
                    # Create new unknown identity
                    next_num = self.get_next_unknown_number()
                    unknown_name = f"Unknown #{next_num}"

                    create_query = """
                        INSERT INTO identities (identity_type, name, metadata)
                        VALUES ('person', %s, %s)
                        RETURNING identity_id
                    """
                    metadata = {
                        'cluster_id': int(cluster_id),
                        'created_by': 'face_clustering',
                        'created_at': datetime.now().isoformat()
                    }

                    cursor.execute(
                        create_query,
                        (unknown_name, extras.Json(metadata))
                    )
                    identity_id = cursor.fetchone()['identity_id']

                    logger.info(
                        f"Created identity '{unknown_name}' ({identity_id}) "
                        f"for cluster {cluster_id}"
                    )

                # Link all embeddings in cluster to this identity
                update_query = """
                    UPDATE embeddings
                    SET identity_id = %s, updated_at = NOW()
                    WHERE embedding_id = ANY(%s)
                """
                cursor.execute(update_query, (identity_id, cluster_embedding_ids))
                updated_count = cursor.rowcount

                logger.info(
                    f"Linked {updated_count} embeddings to identity {identity_id}"
                )

                cluster_map[cluster_id] = identity_id

            conn.commit()
            cursor.close()

        return cluster_map

    def run_clustering(self) -> Dict:
        """
        Main entry point for face clustering workflow.

        Returns:
            Summary statistics dictionary with keys:
            - total_embeddings: Number of embeddings processed
            - clusters_found: Number of distinct clusters
            - noise_points: Number of unclustered (noise) embeddings
            - identities_created: Number of new identities created
            - identities_updated: Number of existing identities updated
        """
        logger.info("Starting face clustering run")

        # 1. Fetch unassigned embeddings
        embedding_ids, vectors = self.get_unassigned_embeddings()

        if len(embedding_ids) == 0:
            return {
                'total_embeddings': 0,
                'clusters_found': 0,
                'noise_points': 0,
                'identities_created': 0,
                'identities_updated': 0
            }

        # 2. Run HDBSCAN clustering
        labels = self.cluster(vectors)

        # 3. Create/update cluster identities
        cluster_map = self.create_cluster_identities(embedding_ids, labels)

        # 4. Calculate statistics
        n_clusters = len(set(labels)) - (1 if -1 in labels else 0)
        n_noise = list(labels).count(-1)

        summary = {
            'total_embeddings': len(embedding_ids),
            'clusters_found': n_clusters,
            'noise_points': n_noise,
            'identities_created': len(cluster_map),
            'identities_updated': 0  # Currently always creates new
        }

        logger.info(f"Face clustering complete: {summary}")
        return summary

    def get_clusters_summary(self) -> List[Dict]:
        """
        Get summary of all face clusters for UI display.

        Returns:
            List of dictionaries with keys:
            - identity_id: UUID of the identity
            - name: Identity name (e.g., "Unknown #5")
            - embedding_count: Number of face embeddings in this cluster
            - sample_images: List of up to 5 sample detection_id references
            - first_seen: Earliest timestamp of any embedding in cluster
            - last_seen: Latest timestamp of any embedding in cluster
        """
        query = """
            SELECT
                i.identity_id,
                i.name,
                COUNT(e.embedding_id) as embedding_count,
                (SELECT ARRAY_AGG(sub.source_image_path) FROM (SELECT DISTINCT e2.source_image_path FROM embeddings e2 WHERE e2.identity_id = i.identity_id AND e2.source_image_path IS NOT NULL ORDER BY e2.source_image_path LIMIT 5) sub) as sample_images,
                MIN(e.created_at) as first_seen,
                MAX(e.created_at) as last_seen
            FROM identities i
            JOIN embeddings e ON i.identity_id = e.identity_id
            WHERE i.name LIKE 'Unknown #%%'
            AND e.embedding_type = 'face'
            GROUP BY i.identity_id, i.name
            ORDER BY i.name
        """

        with get_cursor(commit=False) as cursor:
            cursor.execute(query)
            rows = cursor.fetchall()

        results = []
        for row in rows:
            results.append({
                'identity_id': row['identity_id'],
                'name': row['name'],
                'embedding_count': row['embedding_count'],
                'sample_images': row['sample_images'] or [],
                'first_seen': row['first_seen'].isoformat() if row['first_seen'] else None,
                'last_seen': row['last_seen'].isoformat() if row['last_seen'] else None
            })

        logger.info(f"Retrieved {len(results)} cluster summaries")
        return results

    def assign_cluster(self, identity_id: str, new_name: str) -> bool:
        """
        Assign a human-readable name to a cluster identity.

        Updates the identity name from 'Unknown #N' to the given name,
        allowing users to label clusters once they identify who they represent.

        Args:
            identity_id: UUID of the identity to rename
            new_name: New name for the identity (e.g., "John Doe")

        Returns:
            True if successful, False otherwise
        """
        query = """
            UPDATE identities
            SET name = %s, updated_at = NOW()
            WHERE identity_id = %s
            AND name LIKE 'Unknown #%'
            RETURNING identity_id
        """

        try:
            with get_cursor() as cursor:
                cursor.execute(query, (new_name, identity_id))
                result = cursor.fetchone()

            if result:
                logger.info(f"Renamed identity {identity_id} to '{new_name}'")
                return True
            else:
                logger.warning(
                    f"Could not rename identity {identity_id} - "
                    f"not found or not an Unknown cluster"
                )
                return False
        except Exception as e:
            logger.error(f"Error renaming identity {identity_id}: {e}")
            return False

    def merge_clusters(self, source_identity_id: str, target_identity_id: str) -> bool:
        """
        Merge one cluster into another by reassigning all embeddings.

        All embeddings from source_identity are reassigned to target_identity,
        then the source identity is deleted.

        Args:
            source_identity_id: UUID of identity to merge from (will be deleted)
            target_identity_id: UUID of identity to merge into (will remain)

        Returns:
            True if successful, False otherwise
        """
        try:
            with get_connection() as conn:
                cursor = conn.cursor(cursor_factory=extras.RealDictCursor)

                # Reassign all embeddings
                update_query = """
                    UPDATE embeddings
                    SET identity_id = %s, updated_at = NOW()
                    WHERE identity_id = %s
                """
                cursor.execute(update_query, (target_identity_id, source_identity_id))
                updated_count = cursor.rowcount

                # Delete source identity
                delete_query = """
                    DELETE FROM identities
                    WHERE identity_id = %s
                    AND name LIKE 'Unknown #%'
                """
                cursor.execute(delete_query, (source_identity_id,))
                deleted = cursor.rowcount > 0

                conn.commit()
                cursor.close()

                if deleted:
                    logger.info(
                        f"Merged {updated_count} embeddings from {source_identity_id} "
                        f"into {target_identity_id}"
                    )
                    return True
                else:
                    logger.warning(
                        f"Could not delete source identity {source_identity_id}"
                    )
                    return False

        except Exception as e:
            logger.error(
                f"Error merging {source_identity_id} into {target_identity_id}: {e}"
            )
            return False


if __name__ == '__main__':
    """Standalone mode for manual or cron execution."""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    print("=" * 60)
    print("Face Clustering - Standalone Mode")
    print("=" * 60)

    clusterer = FaceClusterer(min_cluster_size=5, min_samples=3)

    print("\nRunning clustering...")
    summary = clusterer.run_clustering()

    print("\n" + "=" * 60)
    print("CLUSTERING SUMMARY")
    print("=" * 60)
    print(f"Total embeddings processed: {summary['total_embeddings']}")
    print(f"Clusters found:             {summary['clusters_found']}")
    print(f"Noise points:               {summary['noise_points']}")
    print(f"Identities created:         {summary['identities_created']}")
    print(f"Identities updated:         {summary['identities_updated']}")

    print("\n" + "=" * 60)
    print("CLUSTER DETAILS")
    print("=" * 60)

    clusters = clusterer.get_clusters_summary()
    if clusters:
        for cluster in clusters:
            print(f"\n{cluster['name']} ({cluster['identity_id']})")
            print(f"  Embeddings: {cluster['embedding_count']}")
            print(f"  First seen: {cluster['first_seen']}")
            print(f"  Last seen:  {cluster['last_seen']}")
            print(f"  Samples:    {len(cluster['sample_images'])} images")
    else:
        print("\nNo clusters found.")

    print("\n" + "=" * 60)
