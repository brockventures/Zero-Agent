import { Carousel } from './carousel/Carousel';
import { LayoutEditor } from './editor/LayoutEditor';
import { Header } from './shell/Header';
import { VideoStage } from './video/VideoStage';
import { VoiceHud } from './voice/VoiceHud';

export function App() {
  return (
    <div id="mirrormere-app">
      <Header />
      <Carousel />
      <VideoStage />
      <VoiceHud />
      <LayoutEditor />
    </div>
  );
}
