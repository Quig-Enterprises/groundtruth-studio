#!/usr/bin/env python3
"""
Bearing Fault / Vibration Training Script
Trains models on time-range annotation data exported from Groundtruth Studio.

Supports:
- Autoencoder (anomaly detection - learns normal patterns)
- Classifier (supervised - classifies fault types)

Usage:
    python train_bearing_fault.py --train train_samples.parquet --val val_samples.parquet --model autoencoder --epochs 100
    python train_bearing_fault.py --train train_samples.csv --model classifier --epochs 50 --labels "healthy,outer_race,inner_race"
"""

import argparse
import json
import logging
import sys
from pathlib import Path

import numpy as np

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger('train-bearing-fault')


def load_data(file_path):
    """Load data from CSV or Parquet file."""
    file_path = Path(file_path)

    if not file_path.exists():
        raise FileNotFoundError(f"Data file not found: {file_path}")

    if file_path.suffix == '.parquet':
        try:
            import pyarrow.parquet as pq
            table = pq.read_table(file_path)
            df = table.to_pandas()
        except ImportError:
            import pandas as pd
            df = pd.read_parquet(file_path)
    else:
        import pandas as pd
        df = pd.read_csv(file_path)

    logger.info(f"Loaded {len(df)} samples from {file_path}")
    logger.info(f"Columns: {list(df.columns)}")

    return df


def prepare_features(df, label_column='label'):
    """
    Extract features from the dataframe.

    For time-range annotations, we typically have:
    - start_time, end_time, duration
    - video metadata
    - label (fault type or 'healthy')
    - possibly raw signal data or extracted features

    This is a placeholder - real implementation depends on your data format.
    """
    # Basic feature columns that might exist
    feature_cols = []

    # Check for numeric columns that could be features
    numeric_cols = df.select_dtypes(include=[np.number]).columns.tolist()

    # Exclude ID and time columns from features
    exclude_patterns = ['_id', 'video_id', 'sample_id', 'start_time', 'end_time']
    for col in numeric_cols:
        if not any(p in col.lower() for p in exclude_patterns):
            feature_cols.append(col)

    # If we have duration, use it as a feature
    if 'duration' in df.columns:
        feature_cols.append('duration')
        feature_cols = list(set(feature_cols))  # dedupe

    logger.info(f"Using feature columns: {feature_cols}")

    # Get labels
    labels = None
    if label_column in df.columns:
        labels = df[label_column].values
        unique_labels = df[label_column].unique()
        logger.info(f"Labels: {unique_labels}")

    # Extract features (handle missing feature columns gracefully)
    if feature_cols:
        X = df[feature_cols].fillna(0).values
    else:
        # No numeric features found - create dummy features from duration or index
        logger.warning("No numeric feature columns found, using duration/index as placeholder")
        if 'duration' in df.columns:
            X = df[['duration']].fillna(0).values
        else:
            X = np.arange(len(df)).reshape(-1, 1)

    return X, labels, feature_cols


class AutoencoderModel:
    """Simple autoencoder for anomaly detection using numpy/scipy."""

    def __init__(self, input_dim, encoding_dim=8):
        self.input_dim = input_dim
        self.encoding_dim = encoding_dim
        # Simple linear autoencoder weights
        self.encoder_weights = None
        self.decoder_weights = None
        self.mean = None
        self.std = None

    def fit(self, X, epochs=100, learning_rate=0.01):
        """Train the autoencoder."""
        # Normalize
        self.mean = X.mean(axis=0)
        self.std = X.std(axis=0) + 1e-8
        X_norm = (X - self.mean) / self.std

        # Initialize weights
        np.random.seed(42)
        self.encoder_weights = np.random.randn(self.input_dim, self.encoding_dim) * 0.1
        self.decoder_weights = np.random.randn(self.encoding_dim, self.input_dim) * 0.1

        losses = []
        for epoch in range(epochs):
            # Forward pass
            encoded = X_norm @ self.encoder_weights
            decoded = encoded @ self.decoder_weights

            # Compute loss (MSE)
            loss = np.mean((X_norm - decoded) ** 2)
            losses.append(loss)

            # Backward pass (gradient descent)
            error = decoded - X_norm
            grad_decoder = encoded.T @ error / len(X)
            grad_encoder = X_norm.T @ (error @ self.decoder_weights.T) / len(X)

            self.decoder_weights -= learning_rate * grad_decoder
            self.encoder_weights -= learning_rate * grad_encoder

            if (epoch + 1) % 10 == 0:
                logger.info(f"Epoch {epoch + 1}/{epochs}, Loss: {loss:.6f}")

        return losses

    def predict(self, X):
        """Reconstruct inputs and compute reconstruction error."""
        X_norm = (X - self.mean) / self.std
        encoded = X_norm @ self.encoder_weights
        decoded = encoded @ self.decoder_weights
        reconstruction_error = np.mean((X_norm - decoded) ** 2, axis=1)
        return reconstruction_error

    def save(self, path):
        """Save model weights."""
        np.savez(path,
                 encoder_weights=self.encoder_weights,
                 decoder_weights=self.decoder_weights,
                 mean=self.mean,
                 std=self.std,
                 input_dim=self.input_dim,
                 encoding_dim=self.encoding_dim)
        logger.info(f"Model saved to {path}")

    @classmethod
    def load(cls, path):
        """Load model weights."""
        data = np.load(path)
        model = cls(int(data['input_dim']), int(data['encoding_dim']))
        model.encoder_weights = data['encoder_weights']
        model.decoder_weights = data['decoder_weights']
        model.mean = data['mean']
        model.std = data['std']
        return model


class ClassifierModel:
    """Simple softmax classifier using numpy."""

    def __init__(self, input_dim, num_classes, label_names=None):
        self.input_dim = input_dim
        self.num_classes = num_classes
        self.label_names = label_names or [f"class_{i}" for i in range(num_classes)]
        self.weights = None
        self.bias = None
        self.mean = None
        self.std = None

    def _softmax(self, z):
        exp_z = np.exp(z - np.max(z, axis=1, keepdims=True))
        return exp_z / np.sum(exp_z, axis=1, keepdims=True)

    def _encode_labels(self, labels):
        """Convert string labels to one-hot encoding."""
        label_to_idx = {name: i for i, name in enumerate(self.label_names)}
        indices = np.array([label_to_idx.get(l, 0) for l in labels])
        one_hot = np.zeros((len(labels), self.num_classes))
        one_hot[np.arange(len(labels)), indices] = 1
        return one_hot, indices

    def fit(self, X, labels, epochs=100, learning_rate=0.1):
        """Train the classifier."""
        # Normalize
        self.mean = X.mean(axis=0)
        self.std = X.std(axis=0) + 1e-8
        X_norm = (X - self.mean) / self.std

        # Encode labels
        Y, y_indices = self._encode_labels(labels)

        # Initialize weights
        np.random.seed(42)
        self.weights = np.random.randn(self.input_dim, self.num_classes) * 0.1
        self.bias = np.zeros(self.num_classes)

        losses = []
        for epoch in range(epochs):
            # Forward pass
            logits = X_norm @ self.weights + self.bias
            probs = self._softmax(logits)

            # Cross-entropy loss
            loss = -np.mean(np.sum(Y * np.log(probs + 1e-8), axis=1))
            losses.append(loss)

            # Accuracy
            predictions = np.argmax(probs, axis=1)
            accuracy = np.mean(predictions == y_indices)

            # Backward pass
            error = probs - Y
            grad_weights = X_norm.T @ error / len(X)
            grad_bias = np.mean(error, axis=0)

            self.weights -= learning_rate * grad_weights
            self.bias -= learning_rate * grad_bias

            if (epoch + 1) % 10 == 0:
                logger.info(f"Epoch {epoch + 1}/{epochs}, Loss: {loss:.4f}, Accuracy: {accuracy:.4f}")

        return losses

    def predict(self, X):
        """Predict class probabilities."""
        X_norm = (X - self.mean) / self.std
        logits = X_norm @ self.weights + self.bias
        probs = self._softmax(logits)
        return probs

    def predict_labels(self, X):
        """Predict class labels."""
        probs = self.predict(X)
        indices = np.argmax(probs, axis=1)
        return [self.label_names[i] for i in indices]

    def save(self, path):
        """Save model."""
        np.savez(path,
                 weights=self.weights,
                 bias=self.bias,
                 mean=self.mean,
                 std=self.std,
                 input_dim=self.input_dim,
                 num_classes=self.num_classes,
                 label_names=np.array(self.label_names, dtype=object))
        logger.info(f"Model saved to {path}")

    @classmethod
    def load(cls, path):
        """Load model."""
        data = np.load(path, allow_pickle=True)
        model = cls(int(data['input_dim']), int(data['num_classes']),
                    list(data['label_names']))
        model.weights = data['weights']
        model.bias = data['bias']
        model.mean = data['mean']
        model.std = data['std']
        return model


def train_autoencoder(train_df, val_df, epochs, output_dir):
    """Train an autoencoder for anomaly detection."""
    X_train, _, feature_cols = prepare_features(train_df)

    if X_train.shape[1] == 0:
        logger.error("No features to train on")
        return None

    logger.info(f"Training autoencoder with {X_train.shape[1]} features, {X_train.shape[0]} samples")

    encoding_dim = max(2, X_train.shape[1] // 2)
    model = AutoencoderModel(X_train.shape[1], encoding_dim)
    losses = model.fit(X_train, epochs=epochs)

    # Evaluate on validation set if available
    if val_df is not None and len(val_df) > 0:
        X_val, _, _ = prepare_features(val_df)
        val_errors = model.predict(X_val)
        logger.info(f"Validation reconstruction error: mean={val_errors.mean():.6f}, std={val_errors.std():.6f}")

    # Save model
    model_path = output_dir / 'autoencoder_model.npz'
    model.save(str(model_path))

    # Save training info
    info = {
        'model_type': 'autoencoder',
        'input_dim': int(X_train.shape[1]),
        'encoding_dim': encoding_dim,
        'train_samples': len(train_df),
        'val_samples': len(val_df) if val_df is not None else 0,
        'final_loss': float(losses[-1]) if losses else None,
        'feature_columns': feature_cols,
    }
    with open(output_dir / 'training_info.json', 'w') as f:
        json.dump(info, f, indent=2)

    return model


def train_classifier(train_df, val_df, epochs, labels_str, output_dir):
    """Train a classifier for fault type classification."""
    X_train, y_train, feature_cols = prepare_features(train_df)

    if X_train.shape[1] == 0:
        logger.error("No features to train on")
        return None

    if y_train is None:
        logger.error("No labels found in training data")
        return None

    # Determine label names
    if labels_str:
        label_names = [l.strip() for l in labels_str.split(',') if l.strip()]
    else:
        label_names = sorted(list(set(y_train)))

    logger.info(f"Training classifier with {len(label_names)} classes: {label_names}")
    logger.info(f"Features: {X_train.shape[1]}, Samples: {X_train.shape[0]}")

    model = ClassifierModel(X_train.shape[1], len(label_names), label_names)
    losses = model.fit(X_train, y_train, epochs=epochs)

    # Evaluate on validation set
    if val_df is not None and len(val_df) > 0:
        X_val, y_val, _ = prepare_features(val_df)
        predictions = model.predict_labels(X_val)
        accuracy = np.mean(np.array(predictions) == np.array(y_val))
        logger.info(f"Validation accuracy: {accuracy:.4f}")

    # Save model
    model_path = output_dir / 'classifier_model.npz'
    model.save(str(model_path))

    # Save training info
    info = {
        'model_type': 'classifier',
        'input_dim': int(X_train.shape[1]),
        'num_classes': len(label_names),
        'label_names': label_names,
        'train_samples': len(train_df),
        'val_samples': len(val_df) if val_df is not None else 0,
        'final_loss': float(losses[-1]) if losses else None,
        'feature_columns': feature_cols,
    }
    with open(output_dir / 'training_info.json', 'w') as f:
        json.dump(info, f, indent=2)

    return model


def main():
    parser = argparse.ArgumentParser(description='Train bearing fault detection model')
    parser.add_argument('--train', required=True, help='Training data file (CSV or Parquet)')
    parser.add_argument('--val', default='', help='Validation data file (CSV or Parquet)')
    parser.add_argument('--model', default='autoencoder', choices=['autoencoder', 'classifier', 'default'],
                        help='Model type (default: autoencoder)')
    parser.add_argument('--epochs', type=int, default=100, help='Number of training epochs')
    parser.add_argument('--labels', default='', help='Comma-separated label names for classifier')
    parser.add_argument('--output', default='.', help='Output directory for model files')
    parser.add_argument('--verbose', '-v', action='store_true', help='Verbose logging')

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Normalize model type
    model_type = args.model
    if model_type == 'default':
        model_type = 'autoencoder'

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"Loading training data from {args.train}")
    train_df = load_data(args.train)

    val_df = None
    if args.val and args.val != args.train and Path(args.val).exists():
        logger.info(f"Loading validation data from {args.val}")
        val_df = load_data(args.val)

    if model_type == 'autoencoder':
        model = train_autoencoder(train_df, val_df, args.epochs, output_dir)
    else:
        model = train_classifier(train_df, val_df, args.epochs, args.labels, output_dir)

    if model:
        logger.info("Training complete!")
        return 0
    else:
        logger.error("Training failed")
        return 1


if __name__ == '__main__':
    sys.exit(main())
