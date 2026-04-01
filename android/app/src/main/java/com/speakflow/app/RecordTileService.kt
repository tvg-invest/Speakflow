package com.speakflow.app

import android.content.Intent
import android.service.quicksettings.Tile
import android.service.quicksettings.TileService

class RecordTileService : TileService() {

    override fun onClick() {
        // Read state BEFORE sending intent
        val wasRecording = RecordingService.isRecording

        val intent = Intent(this, RecordingService::class.java).apply {
            action = RecordingService.ACTION_TOGGLE
        }
        startForegroundService(intent)

        // Update tile based on pre-toggle state
        qsTile?.let { tile ->
            tile.state = if (wasRecording) Tile.STATE_INACTIVE else Tile.STATE_ACTIVE
            tile.label = if (wasRecording) "SpeakFlow" else "Recording..."
            tile.updateTile()
        }
    }

    override fun onStartListening() {
        updateTile()
    }

    private fun updateTile() {
        qsTile?.let { tile ->
            if (RecordingService.isRecording) {
                tile.state = Tile.STATE_ACTIVE
                tile.label = "Recording..."
            } else {
                tile.state = Tile.STATE_INACTIVE
                tile.label = "SpeakFlow"
            }
            tile.updateTile()
        }
    }
}
