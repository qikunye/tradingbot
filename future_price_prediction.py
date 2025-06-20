import pandas as pd
import numpy as np
import torch
from torch.utils.data import Dataset, DataLoader, random_split
from transformers import (
    TimeSeriesTransformerConfig,
    TimeSeriesTransformerForPrediction,
    Trainer,
    TrainingArguments
)
from sklearn.preprocessing import MinMaxScaler
import joblib
import shutil


# 1. Load and preprocess data
print("DEBUG: Loading CSV data...")
df = pd.read_csv("Download Data - FUND_US_ARCX_SPY.csv")
print(f"DEBUG: Initial data shape: {df.shape}")

df.rename(columns={"Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"}, inplace=True)
print("DEBUG: Columns renamed.")

df['volume'] = df['volume'].str.replace(',', '').astype(int)
for col in ['open', 'high', 'low', 'close']:
    df[col] = df[col].astype(float)
print("DEBUG: Converted price and volume columns to numeric types.")

# Add date-based time features
df['date'] = pd.to_datetime(df['Date'])
df['day_of_week'] = df['date'].dt.dayofweek / 6.0  # scaled [0,1]
df['day_of_month'] = (df['date'].dt.day - 1) / 30.0  # scaled [0,1]
df['month'] = (df['date'].dt.month - 1) / 11.0       # scaled [0,1]
print("DEBUG: Added time features (day_of_week, day_of_month, month).")

# Reverse order for chronological data (oldest first)
df = df.iloc[::-1].reset_index(drop=True)
print("DEBUG: Data reversed for chronological order.")

feature_cols = ['open', 'high', 'low', 'close', 'volume']
time_features = ['day_of_week', 'day_of_month', 'month']

# 2. Normalize data
scaler = MinMaxScaler()
scaled_prices_vol = scaler.fit_transform(df[feature_cols])
joblib.dump(scaler, "transformer_scaler.pkl")
print(f"DEBUG: Scaled numerical features shape: {scaled_prices_vol.shape}")

scaled_data = np.hstack([scaled_prices_vol, df[time_features].values])
print(f"DEBUG: Combined scaled data shape (prices+time features): {scaled_data.shape}")

# 3. Dataset Definition
lookback = 64
horizon = 1


class TimeSeriesDataset(Dataset):
    def __init__(self, data, config, static_categorical, static_real):
        self.config = config
        self.lookback = config.context_length
        self.lags = config.lags_sequence
        self.required_history = self.lookback + max(self.lags)

        print(f"DEBUG: Initializing TimeSeriesDataset with data length: {len(data)}")
        print(f"DEBUG: Required sequence length per sample: {self.required_history}")

        self.X = []
        self.y = []
        self.static_categorical = static_categorical
        self.static_real = static_real

        for i in range(self.required_history, len(data) - config.prediction_length + 1):
            seq = data[i - self.required_history:i]
            target = data[i:i + config.prediction_length]

            self.X.append(torch.tensor(seq, dtype=torch.float32))
            self.y.append(torch.tensor(target, dtype=torch.float32))

        print(f"DEBUG: Dataset tensors created: X shape {[x.shape for x in self.X[:1]]}, total samples: {len(self.X)}")
        
     

    def __len__(self):
        return len(self.X)

    def __getitem__(self, idx):
        past = self.X[idx]  # Shape: [required_history, features]
        future = self.y[idx]  # Shape: [horizon, features]

        # Safety check
        if past.shape[0] < self.required_history:
            raise ValueError(f"past_values too short: {past.shape[0]} < required {self.required_history}")

        sample = {
            "past_values": past,  # Full [context + max_lag] used by model
            "past_time_features": past[:, -self.config.num_time_features:],
            "past_observed_mask": torch.ones((past.shape[0], 1), dtype=torch.bool),
            "future_values": future,
            "future_time_features": future[:, -self.config.num_time_features:],
            "future_observed_mask": torch.ones((future.shape[0], 1), dtype=torch.bool),
            "static_categorical_features": self.static_categorical.clone().unsqueeze(0),
            "static_real_features": self.static_real.clone().unsqueeze(0),
            "labels": future.reshape(self.config.prediction_length, -1),
        }

        if idx < 2:
            print(f"DEBUG: __getitem__ sample {idx} keys and shapes:")
            for k, v in sample.items():
                print(f"  {k}: {v.shape if isinstance(v, torch.Tensor) else type(v)}")

        return sample


# 4. Prepare static features
static_categorical = torch.tensor([0, 1], dtype=torch.long)
static_real = torch.tensor([0.09, 1993.0], dtype=torch.float32)


# 7. Model Configuration
config = TimeSeriesTransformerConfig(
    prediction_length=horizon,
    context_length=60,
    input_size=len(feature_cols) + len(time_features),
    num_time_features=len(time_features),
    num_static_categorical_features=static_categorical.shape[0],
    num_static_real_features=static_real.shape[0],
    cardinality=[2, 2],
    embedding_dimension=[2, 2],
    use_static_features=True,
    lags_sequence=[1,2,3]
)

print(f"DEBUG: Model config - input_size: {config.input_size}, context_length: {config.context_length}")

model = TimeSeriesTransformerForPrediction(config)


dataset = TimeSeriesDataset(
    data=scaled_data,
    config=config,
    static_categorical=static_categorical,
    static_real=static_real
)


# 5. Train/Validation Split
train_size = int(0.8 * len(dataset))
val_size = len(dataset) - train_size
print(f"DEBUG: Dataset split — train size: {train_size}, val size: {val_size}")
train_dataset, val_dataset = random_split(dataset, [train_size, val_size])

# 6. Collate Function
def custom_collate(batch):
    collated = {}
    for key in batch[0].keys():
        stacked = torch.stack([item[key] for item in batch])
        if key in ["static_real_features", "static_categorical_features"]:
            stacked = stacked.squeeze(1)
        collated[key] = stacked
    print(f"DEBUG: custom_collate output keys and shapes: { {k: v.shape for k, v in collated.items()} }")
    return collated

data_loader = DataLoader(train_dataset, batch_size=32, shuffle=True, collate_fn=custom_collate)







# 8. Trainer setup
training_args = TrainingArguments(
    output_dir="./transformer_checkpoints",
    per_device_train_batch_size=32,
    per_device_eval_batch_size=32,
    num_train_epochs=30,
    eval_strategy="epoch",
    save_strategy="epoch",
    logging_dir="./logs",
    logging_steps=20,
    save_total_limit=1,
    load_best_model_at_end=True,
    metric_for_best_model="eval_runtime",
    greater_is_better=False
)

from transformers import EarlyStoppingCallback

trainer = Trainer(
    model=model,
    args=training_args,
    train_dataset=train_dataset,
    eval_dataset=val_dataset,
    data_collator=custom_collate,
    callbacks=[EarlyStoppingCallback(early_stopping_patience=5)],
    
)

print("DEBUG: Starting training...")
trainer.train()
print("DEBUG: Training complete.")

# 10. Save final model
model.save_pretrained("transformer_price_model")
print("DEBUG: Model saved to 'transformer_price_model'.")

print("Sample X shape:", dataset.X[0].shape)
print("Config context length:", config.context_length)
print("Max lag:", max(config.lags_sequence))


