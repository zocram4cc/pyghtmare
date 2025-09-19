#!/usr/bin/env bash

WATCH_DIR="./outputs"
SINK_NAME="virtual_speaker"
SOURCE_NAME="virtual_mic"

# Create folder if missing
mkdir -p "$WATCH_DIR"

# Load virtual speaker (sink)
SINK_MODULE_ID=$(pactl load-module module-null-sink \
    sink_name=$SINK_NAME \
    sink_properties=device.description=$SINK_NAME)
echo "Created sink: $SINK_NAME (module id: $SINK_MODULE_ID)"

# Load virtual mic (remapped source from sink monitor)
SOURCE_MODULE_ID=$(pactl load-module module-remap-source \
    master="${SINK_NAME}.monitor" \
    source_name=$SOURCE_NAME \
    source_properties=device.description=$SOURCE_NAME)
echo "Created source: $SOURCE_NAME (module id: $SOURCE_MODULE_ID)"
#PULSE_SINK=$SINK_NAME ffmpeg -loglevel error -f lavfi -i "anoisesrc=color=white:amplitude=0.0005:sample_rate=44100:nb_samples=1024" \
#    -f pulse "$SINK_NAME" &
#NOISE_PID=$!
#echo "Started background noise (pid=$NOISE_PID)"
# Cleanup on exit
cleanup() {
    echo "Cleaning up..."
#    if [ -n "$NOISE_PID" ]; then
#        kill "$NOISE_PID" 2>/dev/null
#        wait "$NOISE_PID" 2>/dev/null
#        echo "Stopped background noise"
#    fi
    [ -n "$SOURCE_MODULE_ID" ] && pactl unload-module "$SOURCE_MODULE_ID"
    [ -n "$SINK_MODULE_ID" ] && pactl unload-module "$SINK_MODULE_ID"
    echo "Unloaded virtual devices"
    exit 0
}
trap cleanup SIGINT SIGTERM EXIT

echo "Monitoring $WATCH_DIR for new .wav files..."
echo "ffmpeg will play them into $SINK_NAME, apps can use $SOURCE_NAME as microphone"

# Watch for files
inotifywait -m -e close_write --format "%f" "$WATCH_DIR" | while read FILE; do
    if [[ "$FILE" == *.wav ]]; then
        FILEPATH="$WATCH_DIR/$FILE"
        echo "Detected new file: $FILEPATH"
        # Play into the virtual speaker
        PULSE_SINK=$SINK_NAME ffmpeg -loglevel error -y \
        -f lavfi -t 1.25 -i anoisesrc=color=white:amplitude=0.0005:sample_rate=44100:nb_samples=1024 \
        -i "$FILEPATH" \
        -f lavfi -t 2.5 -i anoisesrc=color=white:amplitude=0.0005:sample_rate=44100:nb_samples=1024 \
        -f lavfi -i anoisesrc=color=white:amplitude=0.0002:sample_rate=44100:nb_samples=1024 \
        -filter_complex "[0:a][1:a][2:a]concat=n=3:v=0:a=1[main];[main][3:a]amix=inputs=2:duration=first:dropout_transition=0[out]" \
        -map "[out]" -f pulse "$SINK_NAME" 
	sleep 2
        # Remove after playing
        rm -f "$FILEPATH"
        echo "Played and deleted: $FILEPATH"
    fi
done

