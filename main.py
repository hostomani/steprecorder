#!/usr/bin/env python3
"""
Comprehensive macOS Steps Recorder
Working version with proper imports
"""

import os
import json
import time
import sys
import signal
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Optional, Dict, Any, List
import threading
from enum import Enum
from queue import Queue
import subprocess
from collections import Counter

# Core imports
from Quartz import (
    # Event tap functions
    CGEventTapCreate, CGEventTapEnable,
    kCGHeadInsertEventTap, kCGSessionEventTap, kCGEventTapOptionDefault,
    
    # Event types
    kCGEventLeftMouseDown, kCGEventRightMouseDown, kCGEventOtherMouseDown,
    kCGEventKeyDown, kCGEventKeyUp, kCGEventFlagsChanged,
    kCGEventScrollWheel, kCGEventMouseMoved,
    
    # Event masks
    CGEventMaskBit,
    
    # Event data
    CGEventGetLocation, CGEventGetIntegerValueField,
    CGEventKeyboardGetUnicodeString, CGEventGetType,
    
    # Screenshot
    CGWindowListCreateImage, CGRectInfinite,
    kCGWindowListOptionOnScreenOnly, kCGWindowImageDefault,
    
    # Constants
    kCGEventMaskForAllEvents,
    
    # Additional functions
    CGEventCreateMouseEvent, kCGMouseButtonLeft, kCGMouseButtonRight,
    CGEventPost, kCGHIDEventTap, CGEventSetIntegerValueField,
    kCGKeyboardEventKeycode
)

import CoreFoundation
from CoreFoundation import (
    CFRunLoopAddSource, CFRunLoopGetCurrent, CFRunLoopRun,
    CFRunLoopStop, kCFRunLoopCommonModes,
    CFMachPortCreateRunLoopSource
)

from AppKit import (
    NSWorkspace, NSBitmapImageRep, NSPNGFileType,
    NSRunningApplication
)
import Quartz

# Optional imports
try:
    import pyperclip
    HAS_PYPERCLIP = True
except ImportError:
    HAS_PYPERCLIP = False
    print("⚠️  pyperclip not installed. Clipboard capture disabled.")

try:
    from Quartz.CoreGraphics import CGEventTapIsEnabled
    HAS_TAP_CHECK = True
except ImportError:
    HAS_TAP_CHECK = False
    print("⚠️  CGEventTapIsEnabled not available")

# -----------------------
# Configuration
# -----------------------

class RecorderConfig:
    """Configuration for the steps recorder"""
    def __init__(self):
        self.session_name = datetime.now().strftime("Session_%Y-%m-%d_%H-%M-%S")
        self.output_dir = Path("recordings") / self.session_name
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        # Recording options
        self.capture_screenshots = True
        self.capture_clipboard = True and HAS_PYPERCLIP
        self.capture_keystrokes = True
        self.capture_scroll = True
        self.capture_mouse_moves = False  # Can be verbose
        self.debounce_interval = 0.3  # seconds
        self.max_steps = 1000
        
        # Screenshot settings
        self.screenshot_format = "png"
        
        # UI settings
        self.show_notifications = False
        self.auto_save_interval = 30  # seconds

# -----------------------
# Data Models
# -----------------------

class ActionType(Enum):
    CLICK = "click"
    RIGHT_CLICK = "right_click"
    MIDDLE_CLICK = "middle_click"
    KEY_PRESS = "key_press"
    KEY_COMBO = "key_combo"
    SCROLL = "scroll"
    TEXT_INPUT = "text_input"
    COPY = "copy"
    PASTE = "paste"
    APP_SWITCH = "app_switch"
    SYSTEM = "system"

@dataclass
class UIElement:
    """Represents a UI element with accessibility attributes"""
    role: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    value: Optional[str] = None
    
    def to_dict(self):
        return {k: v for k, v in asdict(self).items() if v is not None}

@dataclass
class ApplicationInfo:
    """Information about the active application"""
    name: str
    bundle_id: Optional[str] = None
    pid: Optional[int] = None
    
    def to_dict(self):
        return asdict(self)

@dataclass
class Step:
    """A recorded step/action"""
    step_number: int
    timestamp: str
    action_type: ActionType
    position: Optional[Dict[str, int]] = None
    element: Optional[Dict[str, Any]] = None
    application: Optional[Dict[str, Any]] = None
    screenshot: Optional[str] = None
    details: Optional[Dict[str, Any]] = None
    
    def __post_init__(self):
        if self.details is None:
            self.details = {}
    
    def to_dict(self):
        return {
            'step_number': self.step_number,
            'timestamp': self.timestamp,
            'action_type': self.action_type.value,
            'position': self.position,
            'element': self.element,
            'application': self.application,
            'screenshot': self.screenshot,
            'details': self.details
        }

# -----------------------
# Recorder Core
# -----------------------

class StepsRecorder:
    def __init__(self, config: RecorderConfig):
        self.config = config
        self.steps: List[Step] = []
        self.step_counter = 1
        self.last_event_time = 0
        self.running = False
        self.event_tap = None
        
        # Setup directories
        (self.config.output_dir / "screenshots").mkdir(exist_ok=True)
        
        # Keyboard state
        self.modifier_keys = {
            56: "shift",      # Shift
            59: "ctrl",       # Control
            58: "alt",        # Option
            55: "cmd"         # Command
        }
        self.pressed_modifiers = set()
        
        print(f"📁 Session: {self.config.session_name}")
        print("Recording started. Press Ctrl+C to stop.")
    
    def start(self):
        """Start the recorder"""
        self.running = True
        
        # Start auto-save thread
        if self.config.auto_save_interval > 0:
            save_thread = threading.Thread(target=self._auto_save, daemon=True)
            save_thread.start()
        
        # Setup event tap
        self._setup_event_tap()
        
        # Record start event
        self._record_system_event("recorder_started")
        
        # Run the main loop
        CFRunLoopRun()
    
    def stop(self):
        """Stop the recorder"""
        print("\n🛑 Stopping recorder...")
        self.running = False
        
        if self.event_tap:
            CGEventTapEnable(self.event_tap, False)
        
        # Record end event
        self._record_system_event("recorder_stopped", {"total_steps": len(self.steps)})
        
        # Save final data
        self.save()
        
        print(f"✅ Recording complete!")
        print(f"📊 Total steps recorded: {len(self.steps)}")
        
        # Generate report
        self._generate_report()
        
        CFRunLoopStop(CFRunLoopGetCurrent())
    
    def _setup_event_tap(self):
        """Setup event tap"""
        # Create event mask
        event_mask = (
            CGEventMaskBit(kCGEventLeftMouseDown) |
            CGEventMaskBit(kCGEventRightMouseDown) |
            CGEventMaskBit(kCGEventKeyDown) |
            CGEventMaskBit(kCGEventFlagsChanged)
        )
        
        if self.config.capture_scroll:
            event_mask |= CGEventMaskBit(kCGEventScrollWheel)
        
        # Create event tap
        self.event_tap = CGEventTapCreate(
            kCGSessionEventTap,
            kCGHeadInsertEventTap,
            kCGEventTapOptionDefault,
            event_mask,
            self._event_callback,
            None
        )
        
        if not self.event_tap:
            print("❌ Failed to create event tap. Please check accessibility permissions.")
            print("Go to: System Preferences > Security & Privacy > Privacy > Accessibility")
            print("Add Terminal or your Python IDE to the list.")
            sys.exit(1)
        
        # Create run loop source
        run_loop_source = CFMachPortCreateRunLoopSource(None, self.event_tap, 0)
        CFRunLoopAddSource(
            CFRunLoopGetCurrent(),
            run_loop_source,
            kCFRunLoopCommonModes
        )
        
        # Enable the tap
        CGEventTapEnable(self.event_tap, True)
        print("✅ Event tap enabled")
    
    def _event_callback(self, proxy, event_type, event, refcon):
        """Event callback function"""
        current_time = time.time()
        
        # Simple debouncing
        if current_time - self.last_event_time < self.config.debounce_interval:
            return event
        
        self.last_event_time = current_time
        
        # Handle different event types
        if event_type == kCGEventLeftMouseDown:
            self._handle_click(event, ActionType.CLICK)
        elif event_type == kCGEventRightMouseDown:
            self._handle_click(event, ActionType.RIGHT_CLICK)
        elif event_type == kCGEventKeyDown:
            self._handle_key_event(event)
        elif event_type == kCGEventFlagsChanged:
            self._handle_modifier_event(event)
        elif event_type == kCGEventScrollWheel and self.config.capture_scroll:
            self._handle_scroll_event(event)
        
        return event
    
    def _handle_click(self, event, action_type: ActionType):
        """Handle mouse click"""
        # Get click location
        location = CGEventGetLocation(event)
        x, y = int(location.x), int(location.y)
        
        # Get application info
        app_info = self._get_active_application()
        
        # Take screenshot
        screenshot = None
        if self.config.capture_screenshots:
            screenshot = self._take_screenshot()
        
        # Create step
        step = Step(
            step_number=self.step_counter,
            timestamp=datetime.now().isoformat(),
            action_type=action_type,
            position={"x": x, "y": y},
            application=app_info.to_dict(),
            screenshot=screenshot,
            details={
                "mouse_button": "left" if action_type == ActionType.CLICK else "right",
                "modifiers": list(self.pressed_modifiers)
            }
        )
        
        self._add_step(step)
        self._print_step(step)
    
    def _handle_key_event(self, event):
        """Handle key press"""
        if not self.config.capture_keystrokes:
            return
        
        # Get key code
        keycode = CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode)
        
        # Skip modifier keys (handled separately)
        if keycode in self.modifier_keys:
            return
        
        # Get key string
        length, key_str = CGEventKeyboardGetUnicodeString(event, 1, None, None)
        if not key_str or length == 0:
            key_str = self._keycode_to_name(keycode)
        
        # Get app info
        app_info = self._get_active_application()
        
        # Check for copy/paste
        if self.config.capture_clipboard:
            if self.pressed_modifiers == {"cmd"} and key_str and key_str.lower() == "c":
                self._handle_copy_action(app_info)
                return
            elif self.pressed_modifiers == {"cmd"} and key_str and key_str.lower() == "v":
                self._handle_paste_action(app_info)
                return
        
        # Determine action type
        if self.pressed_modifiers:
            action_type = ActionType.KEY_COMBO
            key_combo = f"{'+'.join(sorted(self.pressed_modifiers))}+{key_str}"
        else:
            action_type = ActionType.KEY_PRESS
            key_combo = key_str
        
        # Create step
        step = Step(
            step_number=self.step_counter,
            timestamp=datetime.now().isoformat(),
            action_type=action_type,
            application=app_info.to_dict(),
            details={
                "key": key_combo,
                "keycode": int(keycode),
                "modifiers": list(self.pressed_modifiers)
            }
        )
        
        self._add_step(step)
        self._print_step(step)
    
    def _handle_modifier_event(self, event):
        """Handle modifier key changes"""
        keycode = CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode)
        
        if keycode in self.modifier_keys:
            # Check if key is pressed or released
            # This is a simplified check - in real implementation you'd check flags
            modifier_name = self.modifier_keys[keycode]
            
            # Toggle modifier state
            if modifier_name in self.pressed_modifiers:
                self.pressed_modifiers.remove(modifier_name)
            else:
                self.pressed_modifiers.add(modifier_name)
    
    def _handle_scroll_event(self, event):
        """Handle scroll wheel"""
        # Get scroll delta
        scroll_y = CGEventGetIntegerValueField(event, Quartz.kCGScrollWheelEventDeltaAxis1)
        
        if abs(scroll_y) < 0.5:
            return
        
        # Get position
        location = CGEventGetLocation(event)
        x, y = int(location.x), int(location.y)
        
        app_info = self._get_active_application()
        
        step = Step(
            step_number=self.step_counter,
            timestamp=datetime.now().isoformat(),
            action_type=ActionType.SCROLL,
            position={"x": x, "y": y},
            application=app_info.to_dict(),
            details={
                "delta_y": float(scroll_y),
                "direction": "up" if scroll_y > 0 else "down"
            }
        )
        
        self._add_step(step)
        self._print_step(step)
    
    def _handle_copy_action(self, app_info):
        """Handle copy action"""
        if HAS_PYPERCLIP:
            try:
                time.sleep(0.05)  # Small delay for clipboard
                content = pyperclip.paste()
                if content:
                    step = Step(
                        step_number=self.step_counter,
                        timestamp=datetime.now().isoformat(),
                        action_type=ActionType.COPY,
                        application=app_info.to_dict(),
                        details={
                            "content_preview": content[:100],
                            "content_length": len(content)
                        }
                    )
                    self._add_step(step)
                    self._print_step(step)
            except:
                pass
    
    def _handle_paste_action(self, app_info):
        """Handle paste action"""
        step = Step(
            step_number=self.step_counter,
            timestamp=datetime.now().isoformat(),
            action_type=ActionType.PASTE,
            application=app_info.to_dict(),
            details={}
        )
        self._add_step(step)
        self._print_step(step)
    
    def _record_system_event(self, event_type: str, data: Optional[Dict] = None):
        """Record system event"""
        if data is None:
            data = {}
        
        step = Step(
            step_number=self.step_counter,
            timestamp=datetime.now().isoformat(),
            action_type=ActionType.SYSTEM,
            details={"event": event_type, **data}
        )
        self._add_step(step)
        self._print_step(step)
    
    def _keycode_to_name(self, keycode):
        """Convert keycode to readable name"""
        key_names = {
            36: "Enter", 48: "Tab", 49: "Space", 51: "Delete",
            53: "Escape", 76: "Enter", 96: "F5", 97: "F6",
            98: "F7", 99: "F3", 100: "F8", 101: "F9",
            109: "F10", 103: "F11", 111: "F12", 105: "F4",
            107: "F2", 113: "F1", 114: "Help", 115: "Home",
            116: "PageUp", 117: "ForwardDelete", 118: "F4",
            119: "End", 120: "F2", 121: "PageDown", 122: "F1",
            123: "Left", 124: "Right", 125: "Down", 126: "Up"
        }
        return key_names.get(keycode, f"Key_{keycode}")
    
    def _get_active_application(self) -> ApplicationInfo:
        """Get current application"""
        try:
            app = NSWorkspace.sharedWorkspace().frontmostApplication()
            if app:
                return ApplicationInfo(
                    name=str(app.localizedName()) if app.localizedName() else "Unknown",
                    bundle_id=str(app.bundleIdentifier()) if app.bundleIdentifier() else None,
                    pid=int(app.processIdentifier()) if hasattr(app, 'processIdentifier') else None
                )
        except:
            pass
        return ApplicationInfo(name="Unknown")
    
    def _take_screenshot(self) -> Optional[str]:
        """Take and save screenshot"""
        try:
            filename = f"screenshot_{self.step_counter:04d}.png"
            screenshot_path = self.config.output_dir / "screenshots" / filename
            
            img = CGWindowListCreateImage(
                CGRectInfinite,
                kCGWindowListOptionOnScreenOnly,
                0,
                kCGWindowImageDefault
            )
            
            if img:
                rep = NSBitmapImageRep.alloc().initWithCGImage_(img)
                data = rep.representationUsingType_properties_(NSPNGFileType, None)
                if data:
                    data.writeToFile_atomically_(str(screenshot_path), True)
                    return f"screenshots/{filename}"
        except Exception as e:
            print(f"📸 Screenshot failed: {e}")
        return None
    
    def _add_step(self, step: Step):
        """Add step to recording"""
        if len(self.steps) >= self.config.max_steps:
            print("⚠️  Max steps reached")
            self.stop()
            return
        
        self.steps.append(step)
        self.step_counter += 1
        
        # Auto-save periodically
        if len(self.steps) % 5 == 0:
            self.save()
    
    def _print_step(self, step: Step):
        """Print step to console"""
        timestamp = datetime.fromisoformat(step.timestamp).strftime("%H:%M:%S")
        
        if step.action_type in [ActionType.CLICK, ActionType.RIGHT_CLICK]:
            app = step.application.get('name', 'Unknown') if step.application else 'Unknown'
            pos = step.position or {'x': 0, 'y': 0}
            print(f"[{step.step_number:04d}] {timestamp} | {app} | {step.action_type.value.upper()} "
                  f"at ({pos['x']}, {pos['y']})")
        
        elif step.action_type in [ActionType.KEY_PRESS, ActionType.KEY_COMBO]:
            app = step.application.get('name', 'Unknown') if step.application else 'Unknown'
            key = step.details.get('key', 'unknown')
            print(f"[{step.step_number:04d}] {timestamp} | {app} | {step.action_type.value.upper()} | {key}")
        
        elif step.action_type == ActionType.SCROLL:
            direction = step.details.get('direction', 'unknown')
            print(f"[{step.step_number:04d}] {timestamp} | SCROLL {direction}")
        
        elif step.action_type in [ActionType.COPY, ActionType.PASTE]:
            app = step.application.get('name', 'Unknown') if step.application else 'Unknown'
            print(f"[{step.step_number:04d}] {timestamp} | {app} | {step.action_type.value.upper()}")
        
        elif step.action_type == ActionType.SYSTEM:
            event = step.details.get('event', 'system')
            print(f"[{step.step_number:04d}] {timestamp} | SYSTEM | {event}")
    
    def save(self):
        """Save steps to file"""
        output_file = self.config.output_dir / "steps.json"
        
        try:
            steps_data = [step.to_dict() for step in self.steps]
            with open(output_file, 'w') as f:
                json.dump({
                    "session": self.config.session_name,
                    "start_time": self.steps[0].timestamp if self.steps else None,
                    "total_steps": len(self.steps),
                    "steps": steps_data
                }, f, indent=2, default=str)
            
            print(f"💾 Saved {len(self.steps)} steps")
        except Exception as e:
            print(f"Error saving: {e}")
    
    def _auto_save(self):
        """Auto-save thread"""
        while self.running:
            time.sleep(self.config.auto_save_interval)
            if self.steps:
                self.save()
    
    def _generate_report(self):
        """Generate session report"""
        report_file = self.config.output_dir / "report.txt"
        
        try:
            with open(report_file, 'w') as f:
                f.write("=" * 50 + "\n")
                f.write("STEPS RECORDER - SESSION REPORT\n")
                f.write("=" * 50 + "\n\n")
                
                f.write(f"Session: {self.config.session_name}\n")
                f.write(f"Total Steps: {len(self.steps)}\n\n")
                
                # Action counts
                action_counts = Counter()
                for step in self.steps:
                    action_counts[step.action_type.value] += 1
                
                f.write("Action Summary:\n")
                f.write("-" * 30 + "\n")
                for action, count in sorted(action_counts.items()):
                    f.write(f"{action.replace('_', ' ').title():20} {count}\n")
                
                # App usage
                app_counts = Counter()
                for step in self.steps:
                    if step.application and step.application.get('name'):
                        app_counts[step.application['name']] += 1
                
                if app_counts:
                    f.write("\nApplication Usage:\n")
                    f.write("-" * 30 + "\n")
                    for app, count in app_counts.most_common(5):
                        f.write(f"{app[:25]:25} {count}\n")
                
                f.write(f"\nData saved in: {self.config.output_dir}\n")
            
            print(f"📄 Report saved to: {report_file}")
        except Exception as e:
            print(f"Failed to generate report: {e}")

# -----------------------
# Main
# -----------------------

def signal_handler(sig, frame):
    """Handle Ctrl+C"""
    print("\n\n🛑 Stopping recorder...")
    if 'recorder' in globals():
        globals()['recorder'].stop()
    sys.exit(0)

def check_permissions():
    """Check if we have accessibility permissions"""
    print("🔍 Checking permissions...")
    
    # Try to create a simple event tap
    test_tap = CGEventTapCreate(
        kCGSessionEventTap,
        kCGHeadInsertEventTap,
        kCGEventTapOptionDefault,
        CGEventMaskBit(kCGEventKeyDown),
        lambda *args: args[3],  # Simple callback
        None
    )
    
    if test_tap:
        CGEventTapEnable(test_tap, False)
        print("✅ Permissions OK")
        return True
    else:
        print("❌ Accessibility permissions required!")
        print("\nPlease grant permissions:")
        print("1. Open System Preferences")
        print("2. Go to Security & Privacy > Privacy > Accessibility")
        print("3. Click the lock to make changes")
        print("4. Add Terminal (or your IDE) to the list")
        print("5. Restart the application")
        return False

def main():
    """Main entry point"""
    import argparse
    parser = argparse.ArgumentParser(description="macOS Steps Recorder")
    parser.add_argument("--name", help="Session name (used as folder name under recordings/)")
    args = parser.parse_args()

    print("\n" + "="*50)
    print("macOS Steps Recorder")
    print("="*50 + "\n")

    # Check permissions first
    if not check_permissions():
        sys.exit(1)

    # Setup signal handler
    signal.signal(signal.SIGINT, signal_handler)

    # Create config and recorder
    config = RecorderConfig()
    if args.name:
        config.session_name = args.name
        config.output_dir = Path("recordings") / args.name
        config.output_dir.mkdir(parents=True, exist_ok=True)
    global recorder
    recorder = StepsRecorder(config)

    try:
        recorder.start()
    except KeyboardInterrupt:
        recorder.stop()
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()