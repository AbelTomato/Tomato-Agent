import ReactDiffViewer, { DiffMethod } from 'react-diff-viewer-continued';
import { useTheme } from '../hooks/useTheme';

interface ChangesetViewerProps {
  oldValue: string;
  newValue: string;
  fileName?: string;
  language?: string;
}

export const ChangesetViewer = ({ 
  oldValue, 
  newValue, 
  fileName, 
}: ChangesetViewerProps) => {
  const { theme } = useTheme();
  
  return (
    <div className="changeset-viewer">
      {fileName && (
        <div className="text-sm font-mono px-3 py-2 bg-neutral-50 dark:bg-neutral-900 border-b border-neutral-300 dark:border-neutral-700">
          {fileName}
        </div>
      )}
      <ReactDiffViewer
        oldValue={oldValue}
        newValue={newValue}
        splitView={true}
        useDarkTheme={theme === 'dark'}
        compareMethod={DiffMethod.WORDS}
        showDiffOnly={true}
        extraLinesSurroundingDiff={3}
        hideLineNumbers={false}
        styles={{
          variables: {
            dark: {
              diffViewerBackground: '#0d1117',
              diffViewerColor: '#e6edf3',
              addedBackground: '#1a4d2e',
              addedColor: '#7ee787',
              removedBackground: '#5a1a1a',
              removedColor: '#ff7b72',
              wordAddedBackground: '#2ea043',
              wordRemovedBackground: '#da3633',
              addedGutterBackground: '#1a4d2e',
              removedGutterBackground: '#5a1a1a',
              gutterBackground: '#161b22',
              gutterBackgroundDark: '#0d1117',
              highlightBackground: '#3d444d',
              highlightGutterBackground: '#2c3138',
            },
            light: {
              diffViewerBackground: '#ffffff',
              diffViewerColor: '#24292f',
              addedBackground: '#e6ffec',
              addedColor: '#116329',
              removedBackground: '#ffebe9',
              removedColor: '#82071e',
              wordAddedBackground: '#acf2bd',
              wordRemovedBackground: '#ffcecb',
              addedGutterBackground: '#ccffd8',
              removedGutterBackground: '#ffc1bc',
              gutterBackground: '#f6f8fa',
              gutterBackgroundDark: '#f1f3f5',
              highlightBackground: '#fff8c5',
              highlightGutterBackground: '#fff5b1',
            },
          },
        }}
      />
    </div>
  );
};
